# app/extractors/pdf_basic.py
from pathlib import Path
import re
from typing import Optional, Dict, Any, List, Tuple
from dateutil import parser as dateparser
from pdfminer.high_level import extract_text

# pdfplumber optionnel
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# =============== REGEX de base ===============
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

SELLER_BLOCK = re.compile(r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)', re.I | re.S)
CLIENT_BLOCK = re.compile(r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)', re.I | re.S)

# Fallback “tout sur une ligne”
LINE_ONE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Taux TVA
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

# Fallback “colonnes séparées”
AMOUNT_RX = re.compile(r'([0-9]{1,3}(?:[ .][0-9]{3})*(?:[,.][0-9]{2}))')  # 1 000,00
INT_RX    = re.compile(r'^\d{1,4}$')  # 1, 12, 2
REF_LABEL_RX = re.compile(r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+)$')

# =============== Helpers ===============
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

# =============== Extraction des lignes ===============
def _parse_lines_onepass(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in LINE_ONE_RX.finditer(text):
        rows.append({
            "ref":        m.group('ref'),
            "label":      m.group('label').strip(),
            "qty":        int(m.group('qty')),
            "unit_price": _norm_amount(m.group('pu')),
            "amount":     _norm_amount(m.group('amt')),
        })
    return rows

def _parse_lines_by_columns(text: str) -> List[Dict[str, Any]]:
    """ aligne ref/label + Qté + PU + Montant quand les colonnes sont séparées dans le texte """
    lines = [l.strip() for l in text.splitlines()]

    # 1) séquence ref/label
    pairs: List[Tuple[str, str]] = []
    for ln in lines:
        m = REF_LABEL_RX.match(ln)
        if m:
            pairs.append((m.group('ref'), m.group('label').strip()))
    if not pairs:
        return []

    # 2) repères des sections
    def find_index(substrs: List[str]) -> int:
        for i, ln in enumerate(lines):
            lower = ln.lower()
            if any(s in lower for s in substrs):
                return i
        return -1

    i_qte = find_index(['qté', 'qte', 'qty', 'quantité'])
    i_pu  = find_index(['pu', 'prix unitaire', 'unit price'])
    i_amt = find_index(['montant', 'total'])

    qtys: List[int] = []
    pus:  List[Optional[float]] = []
    amts: List[Optional[float]] = []

    if i_qte != -1 and i_pu != -1:
        for ln in lines[i_qte+1:i_pu]:
            ln = ln.replace(' ', '')
            if INT_RX.match(ln):
                try: qtys.append(int(ln))
                except: pass

    if i_pu != -1 and i_amt != -1:
        for ln in lines[i_pu+1:i_amt]:
            m = AMOUNT_RX.search(ln)
            if m: pus.append(_norm_amount(m.group(1)))

    stop_idx = find_index(['total ht'])
    if stop_idx == -1:
        stop_idx = len(lines)
    if i_amt != -1:
        for ln in lines[i_amt+1:stop_idx]:
            m = AMOUNT_RX.search(ln)
            if m: amts.append(_norm_amount(m.group(1)))

    n = min(len(pairs), len(qtys), len(pus), len(amts))
    rows: List[Dict[str, Any]] = []
    for k in range(n):
        ref, label = pairs[k]
        rows.append({
            "ref": ref,
            "label": label,
            "qty": qtys[k],
            "unit_price": pus[k],
            "amount": amts[k],
        })
    return rows

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
                t2 = page.extract_table({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
                if t2: tables.append(t2)

                for tbl in tables:
                    tbl = [[(c or "").strip() for c in (row or [])] for row in (tbl or []) if any((row or []))]
                    if len(tbl) < 2:
                        continue
                    head = [h.lower() for h in tbl[0]]
                    try:
                        i_ref   = next((i for i,h in enumerate(head) if 'réf' in h or 'ref' in h or 'code' in h), None)
                        i_label = next((i for i,h in enumerate(head) if 'désignation' in h or 'designation' in h or 'libellé' in h or 'label' in h), None)
                        i_qty   = next((i for i,h in enumerate(head) if 'qté' in h or 'qte' in h or 'qty' in h), None)
                        i_pu    = next((i for i,h in enumerate(head) if 'pu' in h or 'unit' in h), None)
                        i_amt   = next((i for i,h in enumerate(head) if 'montant' in h or 'total' in h or 'amount' in h), None)
                    except Exception:
                        i_ref = i_label = i_qty = i_pu = i_amt = None

                    if i_label is None:
                        continue

                    for row in tbl[1:]:
                        def get(ix): return row[ix] if ix is not None and ix < len(row) else ''
                        ref   = get(i_ref) or None
                        label = (get(i_label) or '').strip()
                        qty_s = get(i_qty)
                        pu_s  = get(i_pu)
                        amt_s = get(i_amt)

                        qty = None
                        if qty_s:
                            try: qty = int(re.sub(r'[^\d]', '', qty_s))
                            except: qty = None

                        rows.append({
                            "ref":        ref,
                            "label":      label or ref,
                            "qty":        qty,
                            "unit_price": _norm_amount(pu_s),
                            "amount":     _norm_amount(amt_s),
                        })
        # dédoublonnage
        uniq, seen = [], set()
        for r in rows:
            key = (r.get('ref'), r.get('label'), r.get('qty'), r.get('unit_price'), r.get('amount'))
            if key in seen: 
                continue
            seen.add(key)
            uniq.append(r)
        return uniq
    except Exception:
        return []

# =============== Extraction principale ===============
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = extract_text(p) or ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # Numéro / date
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    # Total TTC
    m_total = TOTAL_RE.search(text)
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
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

    # TVA rate
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
            "currency":  currency,
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    f = result["fields"]

    # Vendeur / Acheteur
    m = SELLER_BLOCK.search(text)
    if m and not f.get("seller"):
        f["seller"] = _clean_block(m.group('blk'))
    m = CLIENT_BLOCK.search(text)
    if m and not f.get("buyer"):
        f["buyer"] = _clean_block(m.group('blk'))

    # Identifiants FR vendeur
    m = TVA_RE.search(text)
    if m and not f.get("seller_tva"):
        f["seller_tva"] = m.group(0).replace(' ', '')
    m = SIRET_RE.search(text)
    if m and not f.get("seller_siret"):
        f["seller_siret"] = m.group(0)
    elif not f.get("seller_siret"):
        m2 = SIREN_RE.search(text)
        if m2: f["seller_siret"] = m2.group(0)
    m = IBAN_RE.search(text)
    if m and not f.get("seller_iban"):
        f["seller_iban"] = m.group(0).replace(' ', '')

    # Lignes
    lines: List[Dict[str, Any]] = []
    try:
        lines = _parse_lines_with_pdfplumber(str(p))
    except Exception:
        lines = []
    if not lines:
        lines = _parse_lines_onepass(text)
    if not lines:
        lines = _parse_lines_by_columns(text)

    if lines:
        result["lines"] = lines
        f["lines_count"] = len(lines)
        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2)
        if total_ttc and sum_lines and _approx(sum_lines, total_ttc, tol=1.5):
            th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
            f["total_ht"], f["total_tva"], f["total_ttc"] = th, tv, tt or total_ttc
        else:
            th, tv, tt = _infer_totals(total_ttc, sum_lines or None, None, vat_rate)
            if th is not None: f["total_ht"]  = th
            if tv is not None: f["total_tva"] = tv
            if tt is not None: f["total_ttc"] = tt

    return result
