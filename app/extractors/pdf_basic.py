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

# --- AJOUTS : taux TVA (20/10/5.5) et complétion des totaux ---
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate):
    """
    Complète HT/TVA/TTC si on connaît un taux (ex: 20 -> 0.20).
    Retourne (ht, tva, ttc).
    """
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0

    ht, tva, ttc = total_ht, total_tva, total_ttc

    # Cas 1 : seulement TTC connu
    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht is None:  ht = ht_calc
            if tva is None: tva = tva_calc
        except Exception:
            pass

    # Cas 2 : HT connu
    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception:
            pass

    # Cas 3 : TTC et TVA connus -> calc HT
    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc

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

    # --- Devise plus large ---
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I):
        currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I):
        currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I):
        currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I):
        currency = "USD"

    # --- Taux TVA et complétion HT/TVA ---
    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vat_rate = m_vat.group(1).replace(',', '.')
        if vat_rate in ('5.5', '5,5'):
            vat_rate = '5.5'  # uniformiser

    total_ht = None
    total_tva = None
    if vat_rate is not None:
        total_ht, total_tva, total_ttc = _infer_totals(total_ttc, total_ht, total_tva, vat_rate)

    # -------- Résultat initial --------
    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,   # clé uniforme
            "total_ht": total_ht,
            "total_tva": total_tva,
            "total_ttc": total_ttc,
            "currency": currency or "EUR",
            # seller / buyer / ids FR seront complétés plus bas
        },
        "text": text[:20000],        # utile pour heuristiques & debug (limité)
        "text_preview": text[:2000], # très court pour affichage rapide
    }

    # -------- Heuristiques rapides pour compléter --------
    fields = result["fields"]

    # Seller / Buyer explicites
    m = SELLER_RX.search(text)
    if m and not fields.get("seller"):
        fields["seller"] = m.group(1).strip()

    m = BUYER_RX.search(text)
    if m and not fields.get("buyer"):
        fields["buyer"] = m.group(1).strip()

    # Fallback ultra-simple si seller/buyer absents
    if not fields.get("seller"):
        head = [l.strip() for l in text.splitlines()[:8] if l.strip()]
        if head:
            fields["seller"] = head[0][:120]
    if not fields.get("buyer"):
        m_billto = re.search(r"(?:Bill\s*to|Facturé\s*à|Client)\s*:?\s*(.+)", text, re.I)
        if m_billto:
            fields["buyer"] = m_billto.group(1).strip()[:160]

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
            fields["seller_siret"] = m2.group(0)  # au pire, SIREN

    m = IBAN_RE.search(text)
    if m and not fields.get("seller_iban"):
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # Compter d’éventuelles lignes si tu ajoutes result["lines"] ailleurs
    lines = result.get("lines")
    if isinstance(lines, list) and not fields.get("lines_count"):
        fields["lines_count"] = len(lines)

    return result
