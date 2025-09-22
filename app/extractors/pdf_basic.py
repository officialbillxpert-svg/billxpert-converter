
# app/extractors/pdf_basic.py
from pdfminer.high_level import extract_text
from dateutil import parser as dateparser
import re
from pathlib import Path

NUM_RE = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')
TOTAL_RE = re.compile(r'(?:Total\s*(?:TTC)?|Montant\s*TTC)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)
EUR_RE = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

def _norm_amount(s: str) -> float | None:
    if not s: return None
    s = s.strip()
    # normaliser 1 234,56 / 1.234,56 / 1234.56
    s = s.replace(' ', '')
    if ',' in s and '.' in s:
        # supposer . = séparateur milliers, , = décimales
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None

def extract_pdf(path: str) -> dict:
    p = Path(path)
    text = extract_text(p) or ""
    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": text.count("\f") + 1 if text else 0,
        "filename": p.name,
    }

    # Champs simples
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    date_iso = None
    if m_date:
        try:
            date_iso = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            date_iso = None

    m_total = TOTAL_RE.search(text)
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
    if total_ttc is None:
        # fallback: dernière somme significative du doc
        amounts = [ _norm_amount(a) for a in EUR_RE.findall(text) ]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)

    result = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "date": date_iso,
            "total_ttc": total_ttc,
            # on ajoutera vendeur/client/lignes plus tard
        },
        "text_preview": text[:2000]  # utile pour debug
    }
    return result

