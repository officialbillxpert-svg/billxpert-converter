from __future__ import annotations
import re
from typing import Dict

from .patterns import (
    PATTERNS_VERSION,
    FACTURE_NO_RE, INVOICE_NUM_RE, NUM_RE, DATE_RE,
    TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_AMOUNT_NEAR_RE,
    EUR_STRICT_RE, SELLER_BLOCK, CLIENT_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE,
    EMETTEUR_BLOCK, DESTINATAIRE_BLOCK
)
from .utils_amounts import norm_amount, clean_block, smart_fix_scale


def _parse_date_safe(s: str) -> str | None:
    try:
        from dateutil import parser as dateparser
        raw = re.sub(r'\s*([\/\-.])\s*', r'\1', s)
        return dateparser.parse(raw, dayfirst=True).date().isoformat()
    except Exception:
        return None


def fill_fields_from_text(result: Dict, text: str) -> None:
    fields = result["fields"]

    # ---------- Numéro de facture ----------
    m = FACTURE_NO_RE.search(text or "")
    if not m:
        m = INVOICE_NUM_RE.search(text or "")
    if not m:
        m = NUM_RE.search(text or "")
    fields["invoice_number"] = m.group(1).strip() if m else None

    # ---------- Date ----------
    d = None
    m_date = DATE_RE.search(text or "")
    if m_date:
        d = _parse_date_safe(m_date.group(1))
    fields["invoice_date"] = d

    # ---------- Totaux ----------
    total_ttc = None
    near = TOTAL_TTC_NEAR_RE.findall(text or "")
    if near:
        total_ttc = norm_amount(near[-1])
    if total_ttc is None:
        m_strict = EUR_STRICT_RE.findall(text or "")
        if m_strict:
            total_ttc = norm_amount(m_strict[-1])
    fields["total_ttc"] = total_ttc

    m_ht = TOTAL_HT_NEAR_RE.search(text or "")
    if m_ht:
        ht_val = norm_amount(m_ht.group(1))
        if ht_val is not None:
            fields["total_ht"] = ht_val

    # TVA montant (on évite de capter le %)
    m_tva_amt = TVA_AMOUNT_NEAR_RE.search(text or "")
    if m_tva_amt:
        tva_val = norm_amount(m_tva_amt.group(1))
        if tva_val is not None:
            fields["total_tva"] = tva_val

    # ---------- Currency ----------
    if re.search(r"\bEUR\b|€", text, re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): fields["currency"] = "USD"

    # ---------- Seller / Buyer ----------
    # Émetteur / Vendeur prioritaire
    m = EMETTEUR_BLOCK.search(text or "")
    if m and not fields.get("seller"):
        fields["seller"] = clean_block(m.group('blk'))

    m = DESTINATAIRE_BLOCK.search(text or "")
    if m and not fields.get("buyer"):
        fields["buyer"] = clean_block(m.group('blk'))

    # Fallbacks
    m = SELLER_BLOCK.search(text or "")
    if m and not fields.get("seller"):
        fields["seller"] = clean_block(m.group('blk'))

    m = CLIENT_BLOCK.search(text or "")
    if m and not fields.get("buyer"):
        fields["buyer"] = clean_block(m.group('blk'))

    # ---------- IDs FR ----------
    m = TVA_RE.search(text or "")
    if m and not fields.get("seller_tva"):
        fields["seller_tva"] = m.group(0).replace(' ', '')

    m = SIRET_RE.search(text or "")
    if m and not fields.get("seller_siret"):
        fields["seller_siret"] = m.group(0)
    elif not fields.get("seller_siret"):
        m2 = SIREN_RE.search(text or "")
        if m2:
            fields["seller_siret"] = m2.group(0)

    m = IBAN_RE.search(text or "")
    if m and not fields.get("seller_iban"):
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # ---------- Post-fix cohérence montants ----------
    # Si TTC paraît démesuré, corriger l’échelle si cohérent avec HT+TVA
    fields["total_ttc"] = smart_fix_scale(fields.get("total_ttc"), fields.get("total_ht"), fields.get("total_tva"))

    # Si TVA absente mais HT & TTC présents → TVA = TTC - HT
    if fields.get("total_tva") is None and fields.get("total_ttc") is not None and fields.get("total_ht") is not None:
        diff = round(fields["total_ttc"] - fields["total_ht"], 2)
        if 0 <= diff <= 2_000_000:
            fields["total_tva"] = diff
