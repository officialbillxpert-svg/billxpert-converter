# app/extractors/pdf_basic.py
from pdfminer.high_level import extract_text
from dateutil import parser as dateparser
from pathlib import Path
import re
from typing import Optional, Dict, Any

# --- regex de base ---
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')
TOTAL_RE = re.compile(r'(?:Total\s*(?:TTC)?|Montant\s*TTC)\s*[:€]*\s*([0-9][0-9\.\,\s]+)', re.I)
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# Blocs parties
SELLER_RX = re.compile(r"(?:Émetteur|Vendeur|Seller)\s*:\s*(.+)", re.I)
BUYER_RX  = re.compile(r"(?:Client|Acheteur|Buyer)\s*:\s*(.+)", re.I)


def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(' ', '')
    # normaliser 1 234,56 / 1.234,56 / 1234.56
    if ',' in s and '.' in s:
        # supposer . = milliers, , = décimales
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None


def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = extract_text(p) or ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": text.count("\f") + 1 if text else 0,
        "filename": p.name,
    }

    # -------- Champs simples (numéro, date, total TTC) --------
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    m_total = TOTAL_RE.search(text)
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
    if total_ttc is None:
        # fallback: prendre une “grosse” somme du doc (prudent)
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)

    # -------- Résultat initial --------
    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,   # ⚠ important: clé uniforme "invoice_date"
            "total_ttc": total_ttc,
            # seller / buyer / currency / lines_count seront complétés plus bas
        },
        "text": text[:20000],        # utile pour heuristiques & debug (limité)
        "text_preview": text[:2000], # très court pour affichage rapide
    }

    # -------- Heuristiques rapides pour compléter --------
    fields = result["fields"]

    # Seller / Buyer
    m = SELLER_RX.search(text)
    if m and not fields.get("seller"):
        fields["seller"] = m.group(1).strip()

    m = BUYER_RX.search(text)
    if m and not fields.get("buyer"):
        fields["buyer"] = m.group(1).strip()

    # Devise
    if not fields.get("currency"):
        if re.search(r"\bEUR\b|€", text, re.I):
            fields["currency"] = "EUR"
        elif re.search(r"\bUSD\b|\$", text, re.I):
            fields["currency"] = "USD"

    # Identifiants FR (vendeur)
    m = TVA_RE.search(text)
    if m and not fields.get("seller_tva"):
        fields["seller_tva"] = m.group(0).replace(' ', '')

    m = SIRET_RE.search(text)
    if m and not fields.get("seller_siret"):
        fields["seller_siret"] = m.group(0)
    elif not fields.get("seller_siret"):
        m2 = SIREN_RE.search(text)
        if m2:
            fields["seller_siret"] = m2.group(0)  # faute de mieux, on met le SIREN

    m = IBAN_RE.search(text)
    if m and not fields.get("seller_iban"):
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # Compter d’éventuelles lignes si tu ajoutes result["lines"] ailleurs
    lines = result.get("lines")
    if isinstance(lines, list) and not fields.get("lines_count"):
        fields["lines_count"] = len(lines)

    return result
