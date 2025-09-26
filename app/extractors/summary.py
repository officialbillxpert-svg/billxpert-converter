import re, io, csv, datetime as dt
from typing import Dict, Any, Optional

AMOUNT_RE = re.compile(r'(?<!\d)(?:\d{1,3}(?:[ .]\d{3})*|\d+)(?:[,.]\d{2})?(?!\d)')
DATE_RES = [
    re.compile(r'(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})'),
    re.compile(r'(\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2})'),
]
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

def _norm_amount(s: str) -> Optional[float]:
    if not s: return None
    s = s.strip().replace(' ', '').replace('\u202f','')
    s = s.replace('.', '').replace(',', '.') if s.count(',') >= 1 and s.count('.') <= 1 else s
    try: return float(s)
    except: return None

def _parse_date(s: str) -> Optional[str]:
    s = s.replace(' ', '')
    for rx in DATE_RES:
        m = rx.search(s)
        if not m: continue
        d = m.group(1).replace('.', '/').replace('-', '/')
        parts = d.split('/')
        try:
            if len(parts[0]) == 4:  # YYYY/MM/DD
                t = dt.datetime.strptime(d, '%Y/%m/%d')
            else:                   # DD/MM/YYYY ou DD/MM/YY
                fmt = '%d/%m/%Y' if len(parts[-1]) == 4 else '%d/%m/%y'
                t = dt.datetime.strptime(d, fmt)
            return t.date().isoformat()
        except:
            continue
    return None

def summarize_from_text(text: str) -> Dict[str, Any]:
    out = {
        "invoice_number": None, "invoice_date": None,
        "seller": None, "seller_siret": None, "seller_tva": None, "seller_iban": None,
        "buyer": None,
        "total_ht": None, "total_tva": None, "total_ttc": None,
        "currency": "EUR",
        "lines_count": None,
    }

    # n° facture
    m = re.search(r'(?:facture|invoice)\s*(?:n[°o]\s*|#\s*)?([A-Z0-9\-\/\.]{3,})', text, re.I)
    if m: out["invoice_number"] = m.group(1).strip()

    # date
    out["invoice_date"] = _parse_date(text)

    # SIRET/SIREN/TVA
    m = TVA_RE.search(text);   out["seller_tva"]   = m.group(0).replace(' ', '') if m else None
    m = SIRET_RE.search(text); out["seller_siret"] = m.group(0) if m else (SIREN_RE.search(text).group(0) if SIREN_RE.search(text) else None)

    # IBAN
    m = IBAN_RE.search(text);  out["seller_iban"]  = m.group(0).replace(' ', '') if m else None

    # Totaux
    def grab_total(label_rx):
        m = re.search(label_rx + r'.{0,30}?' + AMOUNT_RE.pattern, text, re.I)
        if not m: return None
        last = AMOUNT_RE.findall(m.group(0))
        return _norm_amount(last[-1]) if last else None

    out["total_ttc"] = grab_total(r'(total\s*(ttc|€)|montant\s+ttc|total\s+amount|grand\s+total)')
    out["total_ht"]  = grab_total(r'(total\s*ht|montant\s+ht|subtotal|sous-total)')
    out["total_tva"] = grab_total(r'(tva|vat\s*total|tax\s*total)')

    return out

def summarize_from_csv(csv_bytes: bytes) -> Dict[str, Any]:
    f = io.StringIO(csv_bytes.decode('utf-8-sig'))
    rd = csv.DictReader(f, delimiter=';')
    total_ht = total_tva = total_ttc = 0.0
    n_lines = 0
    for row in rd:
        n_lines += 1
        ht  = _norm_amount(row.get('total_ht') or row.get('HT') or row.get('total') or '')
        tva = _norm_amount(row.get('tva') or row.get('TVA') or '')
        ttc = _norm_amount(row.get('total_ttc') or row.get('TTC') or '')
        if ht  is not None: total_ht  += ht
        if tva is not None: total_tva += tva
        if ttc is not None: total_ttc += ttc
    return {
        "invoice_number": None, "invoice_date": None,
        "seller": None, "seller_siret": None, "seller_tva": None, "seller_iban": None,
        "buyer": None,
        "total_ht": round(total_ht,2) if total_ht else None,
        "total_tva": round(total_tva,2) if total_tva else None,
        "total_ttc": round(total_ttc,2) if total_ttc else None,
        "currency": "EUR",
        "lines_count": n_lines or None,
    }
