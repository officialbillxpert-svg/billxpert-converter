# app/extractors/summary.py
import re
import io
import csv
import datetime as dt
from typing import Dict, Any, Optional

# Montants type "1 234,56" / "1.234,56" / "1234.56"
AMOUNT_RE = re.compile(r'(?<!\d)(?:\d{1,3}(?:[ .]\d{3})*|\d+)(?:[,.]\d{2})?(?!\d)')

def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(' ', '').replace('\u202f', '')
    # si virgule présente et pas plus d'un point -> on suppose notation FR
    if s.count(',') >= 1 and s.count('.') <= 1:
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except Exception:
        return None

def _parse_date(s: str) -> Optional[str]:
    """Renvoie une date ISO (YYYY-MM-DD) raisonnable trouvée dans s."""
    # normaliser espaces fines
    s = s.replace('\u202f', ' ').replace('\xa0', ' ')

    # candidats numériques (DD/MM/YYYY | DD-MM-YY | YYYY-MM-DD, etc.)
    num = re.findall(
        r'\b(?:\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}|\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2})\b',
        s
    )

    # candidats "25 septembre 2025"
    mois = ('janv|janvier|févr|fevr|février|fevrier|mars|avr|avril|mai|juin|juil|juillet|août|aout|'
            'sept|septembre|oct|octobre|nov|novembre|déc|dec|décembre|decembre')
    mtxt = re.findall(rf'\b(\d{{1,2}})\s+({mois})\s+(\d{{2,4}})\b', s, flags=re.I)

    def month_idx(name: str) -> Optional[int]:
        n = name.lower()[:3]
        table = {
            'jan':1, 'fév':2, 'fev':2, 'mar':3, 'avr':4, 'mai':5, 'jui':6,  # juin
            'jul':7, 'jui':6, 'aoû':8, 'aou':8, 'sep':9, 'oct':10, 'nov':11, 'déc':12, 'dec':12
        }
        # meilleure gestion juil/juin
        if name.lower().startswith('juil'): return 7
        if name.lower().startswith('juin'): return 6
        return table.get(n)

    def norm_numeric(d: str) -> Optional[dt.date]:
        d = d.replace('.', '/').replace('-', '/')
        p = d.split('/')
        try:
            if len(p[0]) == 4:  # YYYY/MM/DD
                t = dt.datetime.strptime(d, '%Y/%m/%d')
            else:               # DD/MM/YYYY or DD/MM/YY
                dd, mm, yy = p[0], p[1], p[2]
                y = int(yy)
                if y < 100:     # 24 -> 2024
                    y = 2000 + y
                t = dt.datetime.strptime(f'{dd}/{mm}/{y}', '%d/%m/%Y')
            if 1990 <= t.year <= 2100:
                return t.date()
        except Exception:
            return None
        return None

    def norm_textual(j: str, mois_str: str, a: str) -> Optional[dt.date]:
        try:
            day = int(j)
            year = int(a)
            if year < 100:
                year = 2000 + year
            mi = month_idx(mois_str)
            if not mi:
                return None
            d = dt.date(year, mi, day)
            if 1990 <= d.year <= 2100:
                return d
        except Exception:
            return None
        return None

    # priorité : date proche du mot "facture"
    near = re.search(
        r'facture.{0,40}?(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}|\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2})',
        s, re.I
    )
    if near:
        d = norm_numeric(near.group(1))
        if d:
            return d.isoformat()

    dates = [norm_numeric(x) for x in num]
    dates = [d for d in dates if d]

    # ajouter les dates en toutes lettres
    for j, mname, a in mtxt:
        d = norm_textual(j, mname, a)
        if d:
            dates.append(d)

    if dates:
        # on retourne la plus récente valide
        return max(dates).isoformat()
    return None

def summarize_from_text(text: str) -> Dict[str, Any]:
    """Heuristiques simples à partir d'un gros texte OCR/texte brut."""
    out = {
        "invoice_number": None, "invoice_date": None,
        "seller": None, "seller_siret": None, "seller_tva": None, "seller_iban": None,
        "buyer": None,
        "total_ht": None, "total_tva": None, "total_ttc": None,
        "currency": "EUR",
        "lines_count": None,
    }

    # N° de facture
    m = re.search(r'(?:facture|invoice)\s*(?:n[°o]\s*|#\s*)?([A-Z0-9\-\/\.]{3,})', text, re.I)
    if m:
        out["invoice_number"] = m.group(1).strip()

    # Date
    out["invoice_date"] = _parse_date(text)

    # Identifiants vendeur
    m = re.search(r'\bFR[A-Z0-9]{2}\s?\d{9}\b', text);  out["seller_tva"]   = m.group(0).replace(' ', '') if m else None
    m = re.search(r'\b\d{14}\b', text) or re.search(r'(?<!\d)\d{9}(?!\d)', text)
    out["seller_siret"] = m.group(0) if m else None

    # IBAN
    m = re.search(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b', text)
    out["seller_iban"] = m.group(0).replace(' ', '') if m else None

    # Totaux
    def grab_total(label_rx):
        m = re.search(label_rx + r'.{0,30}?' + AMOUNT_RE.pattern, text, re.I)
        if not m:
            return None
        last = AMOUNT_RE.findall(m.group(0))
        if not last:
            return None
        v = last[-1].replace(' ', '').replace('\u202f', '')
        if v.count(',') >= 1 and v.count('.') <= 1:
            v = v.replace('.', '').replace(',', '.')
        try:
            return float(v)
        except Exception:
            return None

    out["total_ttc"] = grab_total(r'(total\s*(ttc|€)|montant\s+ttc|total\s+amount|grand\s+total)')
    out["total_ht"]  = grab_total(r'(total\s*ht|montant\s+ht|subtotal|sous-total)')
    out["total_tva"] = grab_total(r'(tva|vat\s*total|tax\s*total)')

    return out
