from __future__ import annotations
import re

from .patterns import (
    FACTURE_NO_RE, INVOICE_NUM_RE, NUM_RE, DATE_RE,
    TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_AMOUNT_NEAR_RE, EUR_STRICT_RE,
    SELLER_BLOCK, CLIENT_BLOCK, EMETTEUR_BLOCK, DESTINATAIRE_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE,
)
from .utils_amounts import _norm_amount, _clean_block

def _fill_fields_from_text(result: dict, text: str) -> None:
    fields = result["fields"]

    # ---- Numéro facture
    m_num = FACTURE_NO_RE.search(text or "") or INVOICE_NUM_RE.search(text or "") or NUM_RE.search(text or "")
    if m_num:
        fields["invoice_number"] = m_num.group(1).strip()

    # ---- Date
    m_date = DATE_RE.search(text or "")
    if m_date:
        try:
            from dateutil import parser as dateparser
            raw = re.sub(r"\s*([\/\-.])\s*", r"\1", m_date.group(1))
            fields["invoice_date"] = dateparser.parse(raw, dayfirst=True).date().isoformat()
        except Exception:
            fields.setdefault("invoice_date", None)

    # ---- Totaux
    total_ttc = None
    near = TOTAL_TTC_NEAR_RE.findall(text or "")
    if near:
        total_ttc = _norm_amount(near[-1])

    if total_ttc is None:
        m_strict = EUR_STRICT_RE.findall(text or "")
        if m_strict:
            total_ttc = _norm_amount(m_strict[-1])

    if total_ttc is not None:
        fields["total_ttc"] = total_ttc

    m_ht = TOTAL_HT_NEAR_RE.search(text or "")
    if m_ht:
        ht_val = _norm_amount(m_ht.group(1))
        if ht_val is not None:
            fields["total_ht"] = ht_val

    m_tva_amt = TVA_AMOUNT_NEAR_RE.search(text or "")
    if m_tva_amt:
        tva_val = _norm_amount(m_tva_amt.group(1))
        if tva_val is not None:
            fields["total_tva"] = tva_val

    # ---- Currency
    if re.search(r"\bEUR\b|€", text or "", re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text or "", re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text or "", re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text or "", re.I): fields["currency"] = "USD"

    # ---- Parties
    if not fields.get("seller"):
        m = SELLER_BLOCK.search(text or "") or EMETTEUR_BLOCK.search(text or "")
        if m:
            fields["seller"] = _clean_block(m.group("blk"))
    if not fields.get("buyer"):
        m = CLIENT_BLOCK.search(text or "") or DESTINATAIRE_BLOCK.search(text or "")
        if m:
            fields["buyer"] = _clean_block(m.group("blk"))

    # ---- IDs FR
    if not fields.get("seller_tva"):
        m = TVA_RE.search(text or "")
        if m: fields["seller_tva"] = m.group(0).replace(" ", "")
    if not fields.get("seller_siret"):
        m = SIRET_RE.search(text or "")
        if m:
            fields["seller_siret"] = m.group(0)
        else:
            m2 = SIREN_RE.search(text or "")
            if m2:
                fields["seller_siret"] = m2.group(0)
    if not fields.get("seller_iban"):
        m = IBAN_RE.search(text or "")
        if m:
            fields["seller_iban"] = m.group(0).replace(" ", "")
