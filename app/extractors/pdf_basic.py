from pathlib import Path
import re
from typing import Optional, Dict, Any, List, Tuple
from dateutil import parser as dateparser
from pdfminer.high_level import extract_text

# pdfplumber est optionnel
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# ---------- Regex de base ----------
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

# Totaux: on capture plutôt les lignes nommées pour éviter de prendre n’importe quelle somme
TOTAL_HT_RE  = re.compile(r'(?:Total\s*HT|Sous-?total)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)
TOTAL_TVA_RE = re.compile(r'(?:TVA(?:\s*\(\s*\d+[.,]?\d*\s*%\s*\))?)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)
TOTAL_TTC_RE = re.compile(r'(?:Total\s*TTC|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)

# fallback montants
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# Blocs parties
SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)

# Lignes “tout sur la même ligne”
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Taux TVA
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

# En-têtes possibles
TABLE_HEADER_HINTS = {
    "label": ("réf", "ref", "reference", "code", "désignation", "designation", "libellé", "description", "label"),
    "qty":   ("qté", "qte", "qty", "quantité", "quantite"),
    "unit":  ("pu", "prix unitaire", "unit price", "price"),
    "amt":   ("montant", "total", "amount")
}

# ---------- Helpers ----------
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

# ---------- Parsing lignes ----------
def _parse_lines_singleline(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in LINE_RX.finditer(text):
        qty = int(m.group('qty'))
        pu  = _norm_amount(m.group('pu'))
        amt = _norm_amount(m.group('amt'))
        rows.append({
            "ref":        m.group('ref'),
            "label":      m.group('label').strip(),
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    return rows

def _find_header_line_idx(lines: List[str], candidates: Tuple[str, ...]) -> Optional[int]:
    for i, raw in enumerate(lines):
        s = raw.strip().lower()
        s = (s.replace("é","e").replace("è","e").replace("ê","e")
               .replace("à","a").replace("û","u").replace("ï","i"))
        for c in candidates:
            if c in s:
                return i
    return None

def _collect_column_values(lines: List[str], start_idx: int) -> List[str]:
    """Collecte les valeurs sous un en-tête jusqu’à un séparateur (ligne vide, 'total', 'merci', etc.)."""
    vals: List[str] = []
    for raw in lines[start_idx+1:]:
        s = raw.strip()
        if not s:
            break
        low = s.lower()
        if low.startswith("total") or "merci" in low or "conditions" in low:
            break
        vals.append(s)
    return vals

def _parse_lines_columns(text: str) -> List[Dict[str, Any]]:
    """
    Fallback pour le cas où les colonnes sont verticales :
    - bloc ‘Réf / Désignation’ (ou équivalent) listé,
    - plus bas bloc ‘Qté’,
    - plus bas bloc ‘PU’,
    - plus bas bloc ‘Montant’.
    """
    lines = [l for l in text.splitlines()]

    idx_label = _find_header_line_idx(lines, TABLE_HEADER_HINTS["label"])
    idx_qty   = _find_header_line_idx(lines, TABLE_HEADER_HINTS["qty"])
    idx_unit  = _find_header_line_idx(lines, TABLE_HEADER_HINTS["unit"])
    idx_amt   = _find_header_line_idx(lines, TABLE_HEADER_HINTS["amt"])

    # On a besoin a minima d’un bloc label + au moins un des autres
    if idx_label is None:
        return []

    labels  = _collect_column_values(lines, idx_label)
    qtys    = _collect_column_values(lines, idx_qty) if idx_qty is not None else []
    units   = _collect_column_values(lines, idx_unit) if idx_unit is not None else []
    amounts = _collect_column_values(lines, idx_amt) if idx_amt is not None else []

    n = len(labels)
    if n == 0:
        return []

    # normalisations
    def to_int_safe(s: str) -> Optional[int]:
        if not s: return None
        m = re.search(r'\d+', s.replace(' ', ''))
        return int(m.group(0)) if m else None

    rows: List[Dict[str, Any]] = []
    for i in range(n):
        label = labels[i].strip()
        # si le label contient "—" on split en ref + libellé (cas "PREST-001 — Dev")
        ref = None
        if '—' in label:
            parts = [p.strip() for p in label.split('—', 1)]
            if len(parts) == 2:
                ref, label = parts[0], parts[1]

        qty  = to_int_safe(qtys[i]) if i < len(qtys) else None
        pu   = _norm_amount(units[i]) if i < len(units) else None
        amt  = _norm_amount(amounts[i]) if i < len(amounts) else None

        rows.append({
            "ref": ref or label,
            "label": label,
            "qty": qty,
            "unit_price": pu,
            "amount": amt
        })

    return rows

def _parse_lines_with_pdfplumber(pdf_path: str) -> List[Dict[str, Any]]:
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                # essaye deux stratégies
                for cfg in (None, {"vertical_strategy":"lines","horizontal_strategy":"lines"}):
                    t = page.extract_table(cfg) if cfg else page.extract_table()
                    if not t:
                        continue
                    table = [[(c or "").strip() for c in (row or [])] for row in t if any((row or []))]
                    if len(table) < 2:
                        continue
                    header = [h.strip().lower() for h in table[0]]
                    # trouve des index “proches”
                    def find_idx(keys: Tuple[str, ...]) -> Optional[int]:
                        for i, h in enumerate(header):
                            hl = h.replace("é","e").replace("è","e").replace("ê","e").replace("à","a")
                            for k in keys:
                                if k in hl:
                                    return i
                        return None
                    i_label = find_idx(TABLE_HEADER_HINTS["label"])
                    i_qty   = find_idx(TABLE_HEADER_HINTS["qty"])
                    i_unit  = find_idx(TABLE_HEADER_HINTS["unit"])
                    i_amt   = find_idx(TABLE_HEADER_HINTS["amt"])
                    if i_label is None:
                        continue
                    for line in table[1:]:
                        def get(i): return line[i] if (i is not None and i < len(line)) else ""
                        label = get(i_label)
                        qty   = get(i_qty)
                        unit  = get(i_unit)
                        amt   = get(i_amt)

                        ref = None
                        if '—' in label:
                            parts = [p.strip() for p in label.split('—',1)]
                            if len(parts)==2:
                                ref, label = parts[0], parts[1]

                        try:
                            qty_i = int(re.search(r'\d+', qty.replace(' ', '')).group(0)) if qty else None
                        except Exception:
                            qty_i = None
                        rows.append({
                            "ref": ref or (label or None),
                            "label": label or None,
                            "qty": qty_i,
                            "unit_price": _norm_amount(unit),
                            "amount": _norm_amount(amt)
                        })
        # dédoublonnage simple
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen: 
                continue
            seen.add(key)
            uniq.append(r)
        return uniq
    except Exception:
        return []

# ---------- Extraction principale ----------
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = extract_text(p) or ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # Numéro & date
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    # Totaux (priorité aux lignes nommées)
    total_ht  = None
    total_tva = None
    total_ttc = None

    m = TOTAL_HT_RE.search(text)
    if m: total_ht = _norm_amount(m.group(1))
    m = TOTAL_TVA_RE.search(text)
    if m: total_tva = _norm_amount(m.group(1))
    m = TOTAL_TTC_RE.search(text)
    if m: total_ttc = _norm_amount(m.group(1))

    # fallback TTC si rien
    if total_ttc is None:
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)

    # Devise
    currency = "EUR"
    if re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"

    # Taux TVA
    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vr = m_vat.group(1)
        vat_rate = '5.5' if vr in ('5,5', '5.5') else vr

    # Base résultat
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
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))
    m = CLIENT_BLOCK.search(text)
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

    # Identifiants vendeur
    m = TVA_RE.search(text)
    if m and not fields.get("seller_tva"):
        fields["seller_tva"] = m.group(0).replace(' ', '')
    m = SIRET_RE.search(text)
    if m and not fields.get("seller_siret"):
        fields["seller_siret"] = m.group(0)
    elif not fields.get("seller_siret"):
        m2 = SIREN_RE.search(text)
        if m2:
            fields["seller_siret"] = m2.group(0)
    m = IBAN_RE.search(text)
    if m and not fields.get("seller_iban"):
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # Lignes d’articles : pdfplumber → singleline → colonnes
    lines: List[Dict[str, Any]] = []
    try:
        lines = _parse_lines_with_pdfplumber(str(p))
    except Exception:
        lines = []
    if not lines:
        lines = _parse_lines_singleline(text)
    if not lines:
        lines = _parse_lines_columns(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        # Si HT/TVA manquants, on tente depuis lignes + taux
        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None
        if sum_lines and total_ttc and _approx(sum_lines, total_ttc, tol=1.5):
            h, t, tt = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = fields.get("total_ht")  or h
            fields["total_tva"] = fields.get("total_tva") or t
            fields["total_ttc"] = fields.get("total_ttc") or tt or total_ttc
        else:
            # suppose les montants-lignes = HT si cohérent
            if sum_lines and fields.get("total_ht") is None:
                fields["total_ht"] = sum_lines
            h, t, tt = _infer_totals(fields.get("total_ttc"), fields.get("total_ht"), fields.get("total_tva"), vat_rate)
            fields["total_ht"]  = fields.get("total_ht")  or h
            fields["total_tva"] = fields.get("total_tva") or t
            fields["total_ttc"] = fields.get("total_ttc") or tt

    return result
