# app/extractors/pdf_basic.py
import logging
from pathlib import Path
import re
from typing import Optional, Dict, Any, List, Tuple

# pdfminer (texte brut fiable)
from pdfminer.high_level import extract_text as _extract_text

# date parsing
from dateutil import parser as dateparser

# pdfplumber (tableaux quand dispo)
try:
    import pdfplumber
except Exception:  # Render peut builder sans cette lib
    pdfplumber = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bx.extract")

# ───────────────────────────────── Regex de base ─────────────────────────────────

INVOICE_NO_RE = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/\.]{2,})', re.I)
DATE_RE       = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

# ⚠️ 3 regex séparés pour éviter toute confusion
TOTAL_HT_RE   = re.compile(r'(?:Total\s*HT|Sous-?total|Subtotal)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)
TOTAL_TVA_RE  = re.compile(r'(?:TVA|VAT)(?:\s*\(\s*\d+(?:[.,]\d+)?\s*%\s*\))?\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)
TOTAL_TTC_RE  = re.compile(r'(?:Total\s*TTC|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)

# “nombres euros” générique
MONEY_RE = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR (vendeur)
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_ID_RE = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# Blocs parties
SELLER_BLOCK = re.compile(r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)', re.I | re.S)
CLIENT_BLOCK = re.compile(r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)', re.I | re.S)

# Lignes “propres” (quand tout est sur 1 ligne)
LINE_ONE_RE = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Taux TVA (pour compléter totaux)
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*\(\s*(20|10|5[.,]?5)\s*%\s*\)', re.I)

# ──────────────────────────────── Helpers généraux ───────────────────────────────

def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None

def _clean_block(s: str) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None

def _approx(a: Optional[float], b: Optional[float], tol: float = 1.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Complète HT/TVA/TTC à partir d’un taux s’il manque des valeurs."""
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0

    ht, tva, ttc = total_ht, total_tva, total_ttc

    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht is None:  ht = ht_calc
            if tva is None: tva = tva_calc
        except Exception:
            pass

    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception:
            pass

    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc

# ───────────────────────────── pdfplumber (tableaux) ─────────────────────────────

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

def _norm_header_cell(s: str) -> str:
    s = (s or "").strip().lower()
    s = (s.replace("é", "e").replace("è", "e").replace("ê", "e")
           .replace("à", "a").replace("û", "u").replace("ï", "i"))
    s = re.sub(r'\s+', ' ', s)
    return s

def _map_header_indices(headers: List[str]) -> Optional[Dict[str, int]]:
    norm = [_norm_header_cell(h) for h in headers]
    def find(*cands):
        for i, h in enumerate(norm):
            for c in cands:
                if c in h:
                    return i
        return None
    idx = {
        "ref":    find(*TABLE_HEADER_HINTS[0]),
        "label":  find(*TABLE_HEADER_HINTS[1]),
        "qty":    find(*TABLE_HEADER_HINTS[2]),
        "unit":   find(*TABLE_HEADER_HINTS[3]),
        "amount": find(*TABLE_HEADER_HINTS[4]),
    }
    if all(v is None for v in idx.values()):
        return None
    return {k: v for k, v in idx.items() if v is not None}

def _parse_lines_with_pdfplumber(pdf_path: str) -> List[Dict[str, Any]]:
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = []
                t = page.extract_table()
                if t:  tables.append(t)
                t2 = page.extract_table({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
                if t2: tables.append(t2)

                for tbl in tables:
                    tbl = [[(c or "").strip() for c in (row or [])] for row in (tbl or []) if any((row or []))]
                    if not tbl or len(tbl) < 2:
                        continue
                    idx = _map_header_indices(tbl[0])
                    if not idx:
                        continue

                    for line in tbl[1:]:
                        def get(i): return line[i] if (i is not None and i < len(line)) else ""
                        ref   = get(idx.get("ref"))
                        label = get(idx.get("label")) or ref
                        qty   = get(idx.get("qty"))
                        pu    = get(idx.get("unit"))
                        amt   = get(idx.get("amount"))

                        try:
                            qty = int(re.sub(r"[^\d]", "", qty)) if qty else None
                        except Exception:
                            qty = None

                        pu_f  = _norm_amount(pu)
                        amt_f = _norm_amount(amt)
                        if not (label or pu_f is not None or amt_f is not None):
                            continue

                        rows.append({
                            "ref":        (ref or "").strip() or None,
                            "label":      (label or "").strip(),
                            "qty":        qty,
                            "unit_price": pu_f,
                            "amount":     amt_f
                        })
        # dédoublonnage
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen: 
                continue
            seen.add(key)
            uniq.append(r)
        return uniq
    except Exception as e:
        log.warning("pdfplumber failed: %s", e)
        return []

# ─────────────┐
#  Heuristique │ lignes “cassées” (texte brut : libellé puis nombres)
# ─────────────┘
def _parse_lines_from_text_stream(text: str) -> List[Dict[str, Any]]:
    """
    On cherche une ligne “nommée” (PREST-001 — Libellé) puis on regarde les
    6–8 tokens suivants pour récupérer qty / PU / montant dans l’ordre d’apparition.
    """
    lines: List[Dict[str, Any]] = []

    # 1) cas simple (tout sur une ligne)
    for m in LINE_ONE_RE.finditer(text):
        qty = int(m.group('qty'))
        pu  = _norm_amount(m.group('pu'))
        amt = _norm_amount(m.group('amt'))
        lines.append({
            "ref":        m.group('ref'),
            "label":      m.group('label').strip(),
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    if lines:
        return lines

    # 2) cas “cassé”
    label_rx = re.compile(r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+)$', re.M)
    all_lines = list(label_rx.finditer(text))
    if not all_lines:
        return []

    # pré-tokenisation des nombres & entiers
    numbers = [(_m.group(0), _m.start()) for _m in MONEY_RE.finditer(text)]
    ints    = [(m.group(0), m.start()) for m in re.finditer(r'\b\d{1,3}\b', text)]

    def next_tokens(pos: int, limit_chars: int = 200) -> List[str]:
        """Retourne les nombres trouvés juste après `pos` dans une fenêtre courte."""
        end = pos + limit_chars
        toks = [val for (val, p) in numbers if pos < p <= end]
        # garder aussi petits entiers (quantités)
        ints_toks = [val for (val, p) in ints if pos < p <= end]
        # merge en gardant l’ordre d’apparition :
        merged = []
        idx_num = 0
        idx_int = 0
        # recrée un flux ordonné en comparant positions
        positions = sorted([(p, val, 'n') for (val, p) in numbers if pos < p <= end] +
                           [(p, val, 'i') for (val, p) in ints if pos < p <= end],
                           key=lambda x: x[0])
        for _p, val, _t in positions:
            merged.append(val)
        return merged

    for i, m in enumerate(all_lines):
        ref = m.group('ref').strip()
        label = m.group('label').strip()
        start_here = m.end()
        # borne : avant le prochain libellé ou la zone “Total”
        stop = all_lines[i + 1].start() if i + 1 < len(all_lines) else text.find("Total", start_here)
        if stop == -1:
            stop = start_here + 200
        toks = next_tokens(start_here, limit_chars=max(80, min(220, stop - start_here)))

        # Heuristique : qty = 1er entier simple (<=999) ; PU = 1er montant à décimales ; montant = suivant
        qty = None
        unit = None
        amt = None
        for t in toks:
            if qty is None and re.fullmatch(r'\d{1,3}', t):
                qty = int(t)
                continue
            if unit is None and _norm_amount(t) not in (None, 0) and re.search(r'[,.]\d{2}', t):
                unit = _norm_amount(t)
                continue
            if unit is not None and amt is None and _norm_amount(t) not in (None, 0):
                amt = _norm_amount(t)
                break

        lines.append({
            "ref": ref,
            "label": label,
            "qty": qty,
            "unit_price": unit,
            "amount": amt
        })

    # filtre lignes totalement vides
    lines = [r for r in lines if any([r.get("qty"), r.get("unit_price"), r.get("amount")])]
    return lines

# ─────────────────────────────── Extraction principale ───────────────────────────

def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = _extract_text(p) or ""
    log.info("PDF parsed: %s (%d chars)", p.name, len(text))

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # Champs simples
    invoice_number = None
    m = INVOICE_NO_RE.search(text)
    if m:
        invoice_number = m.group(1).strip()

    invoice_date = None
    m = DATE_RE.search(text)
    if m:
        try:
            invoice_date = dateparser.parse(m.group(1), dayfirst=True).date().isoformat()
        except Exception:
            pass

    # Totaux (séparés)
    total_ht = None
    total_tva = None
    total_ttc = None

    m = TOTAL_HT_RE.search(text)
    if m:
        total_ht = _norm_amount(m.group(1))

    m = TOTAL_TVA_RE.search(text)
    if m:
        total_tva = _norm_amount(m.group(1))  # uniquement si c’est un nombre €

    m = TOTAL_TTC_RE.search(text)
    if m:
        total_ttc = _norm_amount(m.group(1))
    if total_ttc is None:
        # fallback : plus grande somme du doc
        amounts = [_norm_amount(a) for a in MONEY_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)

    # Devise
    currency = "EUR"
    if re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"
    elif re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"

    # Taux TVA (pour compléter)
    vat_rate = None
    m = VAT_RATE_RE.search(text)
    if m:
        vr = m.group(1).replace(',', '.')
        vat_rate = '5.5' if vr in ('5.5', '5,5') else vr

    # Résultat initial
    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "total_ht":  total_ht,
            "total_tva": total_tva,
            "total_ttc": total_ttc,
            "currency":  currency,
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # Seller / Buyer
    m = SELLER_BLOCK.search(text)
    if m:
        fields["seller"] = _clean_block(m.group('blk'))
    m = CLIENT_BLOCK.search(text)
    if m:
        fields["buyer"] = _clean_block(m.group('blk'))

    # Identifiants (vendeur)
    m = TVA_ID_RE.search(text)
    if m:
        fields["seller_tva"] = m.group(0).replace(' ', '')
    m = SIRET_RE.search(text) or SIREN_RE.search(text)
    if m:
        fields["seller_siret"] = m.group(0)
    m = IBAN_RE.search(text)
    if m:
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # Lignes d’articles
    lines: List[Dict[str, Any]] = []
    # 1) pdfplumber si dispo
    try:
        lines = _parse_lines_with_pdfplumber(str(p))
    except Exception:
        lines = []
    # 2) heuristique texte
    if not lines:
        lines = _parse_lines_from_text_stream(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        # Si on n’a pas HT/TVA mais on a un taux → complète
        sum_lines = round(sum((r.get("amount") or 0) for r in lines), 2)
        if total_ttc and sum_lines and _approx(sum_lines, total_ttc, tol=1.5):
            ht, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
            if fields.get("total_ht") is None:  fields["total_ht"]  = ht
            if fields.get("total_tva") is None: fields["total_tva"] = tv
            fields["total_ttc"] = tt or total_ttc
        else:
            # Considère les montants lignes comme HT par défaut
            if sum_lines and fields.get("total_ht") is None:
                fields["total_ht"] = sum_lines
            ht, tv, tt = _infer_totals(fields.get("total_ttc"), fields.get("total_ht"), fields.get("total_tva"), vat_rate)
            fields["total_ht"], fields["total_tva"], fields["total_ttc"] = ht, tv, tt

    return result
