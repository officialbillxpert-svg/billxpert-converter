# app/extractors/pdf_basic.py
from pdfminer.high_level import extract_text
from dateutil import parser as dateparser
from pathlib import Path
import re
from typing import Optional, Dict, Any, List, Tuple

# pdfplumber optionnel (on fonctionne sans)
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# --------- REGEX de base ---------
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')
TOTAL_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Montant\s*TTC|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer|Bill\s*to)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)

# fallback “tout-en-une-ligne”
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

# --------- HELPERS ---------
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
            ht = ht if ht is not None else ht_calc
            tva = tva if tva is not None else tva_calc
        except Exception:
            pass

    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            tva = tva if tva is not None else tva_calc
            ttc = ttc if ttc is not None else ttc_calc
        except Exception:
            pass

    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc

# ---- pdfplumber helpers (optionnels) ----
def _norm_header_cell(s: str) -> str:
    s = (s or "").strip().lower()
    s = (s.replace("é","e").replace("è","e").replace("ê","e")
           .replace("à","a").replace("û","u").replace("ï","i"))
    s = s.replace("\n"," ").replace("\t"," ")
    s = re.sub(r"\s+"," ", s)
    return s

def _map_header_indices(headers: List[str]) -> Optional[Dict[str, int]]:
    idx: Dict[str, Optional[int]] = {}
    norm = [_norm_header_cell(h) for h in headers]

    def match_one(*cands):
        for i, h in enumerate(norm):
            for c in cands:
                if c in h:
                    return i
        return None

    idx["ref"]    = match_one(*TABLE_HEADER_HINTS[0])
    idx["label"]  = match_one(*TABLE_HEADER_HINTS[1])
    idx["qty"]    = match_one(*TABLE_HEADER_HINTS[2])
    idx["unit"]   = match_one(*TABLE_HEADER_HINTS[3])
    idx["amount"] = match_one(*TABLE_HEADER_HINTS[4])

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
                if t: tables.append(t)
                t2 = page.extract_table({"vertical_strategy":"lines", "horizontal_strategy":"lines"})
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
                        pu_f, amt_f = _norm_amount(pu), _norm_amount(amt)
                        if not (label or pu_f is not None or amt_f is not None):
                            continue
                        rows.append({
                            "ref": (ref or "").strip() or None,
                            "label": (label or "").strip(),
                            "qty": qty,
                            "unit_price": pu_f,
                            "amount": amt_f
                        })
        # dédoublonnage
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen: continue
            seen.add(key); uniq.append(r)
        return uniq
    except Exception:
        return []

# ---- Fallback lignes multi-lignes avec pdfminer ----
def _parse_lines_text_multiline(lines: List[str]) -> List[Dict[str, Any]]:
    """
    Cherche des lignes de type:
      'PREST-001 — Développement'
      '1    1 000,00 €    1 000,00 €'
    ou variantes (espaces variables).
    """
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        l = lines[i]
        m = re.match(r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+)$', l)
        if m:
            ref = m.group('ref').strip()
            label = m.group('label').strip()
            # cherche qté/pu/amt sur 1 ou 2 lignes suivantes
            j = i + 1
            qty = None; pu = None; amt = None
            for _ in range(2):
                if j >= len(lines): break
                l2 = lines[j]
                # ex: "1    1 000,00 €    1 000,00 €"
                m2 = re.search(r'(?P<qty>\d{1,3})\s+([0-9\.\,\s]+€?)\s+([0-9\.\,\s]+€?)', l2)
                if m2:
                    qty = int(m2.group('qty'))
                    # deux derniers nombres de la ligne
                    nums = re.findall(r'([0-9][0-9\.\,\s]+)', l2)
                    if len(nums) >= 2:
                        pu  = _norm_amount(nums[-2])
                        amt = _norm_amount(nums[-1])
                    break
                j += 1
            out.append({
                "ref": ref, "label": label,
                "qty": qty, "unit_price": pu, "amount": amt
            })
            i = j + 1
            continue
        i += 1
    return out

def _parse_lines_text_one_line(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in LINE_RX.finditer(text):
        rows.append({
            "ref": m.group('ref'),
            "label": m.group('label').strip(),
            "qty": int(m.group('qty')),
            "unit_price": _norm_amount(m.group('pu')),
            "amount": _norm_amount(m.group('amt')),
        })
    return rows

# --------- Extraction principale ---------
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = extract_text(p) or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # Numéro / Date / Total TTC
    invoice_number = (NUM_RE.search(text).group(1).strip()
                      if NUM_RE.search(text) else None)

    invoice_date = None
    m_date = DATE_RE.search(text)
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    total_ttc = _norm_amount(TOTAL_RE.search(text).group(1)) if TOTAL_RE.search(text) else None
    if total_ttc is None:
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts: total_ttc = max(amounts)

    # Devise
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"

    # Taux TVA
    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vr = m_vat.group(1)
        vat_rate = '5.5' if vr in ('5,5', '5.5') else vr

    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "total_ht":  None,
            "total_tva": None,
            "total_ttc": total_ttc,
            "currency":  currency or "EUR",
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # Seller / Buyer via blocs
    m = SELLER_BLOCK.search(text)
    if m: fields["seller"] = fields.get("seller") or _clean_block(m.group('blk'))
    m = CLIENT_BLOCK.search(text)
    if m: fields["buyer"] = fields.get("buyer") or _clean_block(m.group('blk'))

    # Si pas trouvé, heuristique par proximité dans les lignes
    def _grab_block(after_keywords: List[str], stop_keywords: List[str]) -> Optional[str]:
        ak = re.compile("|".join([re.escape(k) for k in after_keywords]), re.I)
        sk = re.compile("|".join([re.escape(k) for k in stop_keywords]), re.I)
        for i, ln in enumerate(lines):
            if ak.search(ln):
                buf = [ln]
                for j in range(i+1, min(i+8, len(lines))):
                    if sk.search(lines[j]): break
                    if re.match(r'^(Réf|Ref|Designation|Désignation|Qté|Montant|Total)\b', lines[j], re.I): break
                    buf.append(lines[j])
                return _clean_block(" ".join(buf))
        return None

    if not fields.get("seller"):
        blk = _grab_block(["Émetteur", "Vendeur", "Seller"], ["Client","Acheteur","Buyer"])
        if blk: fields["seller"] = blk
    if not fields.get("buyer"):
        blk = _grab_block(["Client","Acheteur","Buyer","Bill to"], ["Émetteur","Vendeur","Seller"])
        if blk: fields["buyer"] = blk

    # Identifiants FR & IBAN (vendeur)
    m = TVA_RE.search(text)
    if m and not fields.get("seller_tva"): fields["seller_tva"] = m.group(0).replace(' ', '')
    m = SIRET_RE.search(text)
    if m and not fields.get("seller_siret"): fields["seller_siret"] = m.group(0)
    elif not fields.get("seller_siret"):
        m2 = SIREN_RE.search(text)
        if m2: fields["seller_siret"] = m2.group(0)
    m = IBAN_RE.search(text)
    if m and not fields.get("seller_iban"): fields["seller_iban"] = m.group(0).replace(' ', '')

    # -------- LIGNES D'ARTICLES --------
    # 1) pdfplumber (si dispo)
    article_lines: List[Dict[str, Any]] = []
    try:
        article_lines = _parse_lines_with_pdfplumber(str(p))
    except Exception:
        article_lines = []
    # 2) texte: une seule ligne
    if not article_lines:
        article_lines = _parse_lines_text_one_line(text)
    # 3) texte: multi-lignes autour de “ref — label” + “qty PU montant”
    if not article_lines:
        article_lines = _parse_lines_text_multiline(lines)

    if article_lines:
        result["lines"] = article_lines
        fields["lines_count"] = len(article_lines)

        sum_lines = round(sum((r.get("amount") or 0.0) for r in article_lines), 2)
        if total_ttc and _approx(sum_lines, total_ttc, 1.5):
            th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"], fields["total_tva"], fields["total_ttc"] = th, tv, tt or total_ttc
        else:
            th, tv, tt = _infer_totals(total_ttc, sum_lines or None, None, vat_rate)
            if th is not None: fields["total_ht"] = th
            if tv is not None: fields["total_tva"] = tv
            if tt is not None: fields["total_ttc"] = tt

    return result
