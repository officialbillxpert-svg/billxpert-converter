from __future__ import annotations
import re
from typing import Dict, Optional

from .patterns import (
    INVOICE_NUM_RE, NUM_RE, DATE_RE, TOTAL_TTC_NEAR_RE, EUR_STRICT_RE,
    TOTAL_HT_NEAR_RE, TVA_NEAR_RE, SELLER_BLOCK, CLIENT_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE, VAT_RATE_RE
)
from .utils_amounts import norm_amount, clean_block

def extract_vat_rate(text: str) -> Optional[str]:
    m_vat = VAT_RATE_RE.search(text or "")
    if not m_vat:
        return None
    vr = m_vat.group(1)
    return '5.5' if vr in ('5,5', '5.5') else vr

def fill_fields_from_text(result: Dict[str, any], text: str) -> None:
    fields = result["fields"]

    # invoice number
    m_num = INVOICE_NUM_RE.search(text or "") or NUM_RE.search(text or "")
    fields["invoice_number"] = m_num.group(1).strip() if m_num else None

    # date
    m_date = DATE_RE.search(text or "")
    if m_date:
        try:
            from dateutil import parser as dateparser
            fields["invoice_date"] = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            fields["invoice_date"] = None

    # totals
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

    m_tva = TVA_NEAR_RE.search(text or "")
    if m_tva:
        tva_val = norm_amount(m_tva.group(1))
        if tva_val is not None:
            fields["total_tva"] = tva_val

    # currency
    if re.search(r"\bEUR\b|€", text, re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): fields["currency"] = "USD"

    # blocks
    m = SELLER_BLOCK.search(text or "")
    if m and not fields.get("seller"):
        fields["seller"] = clean_block(m.group('blk'))

    m = CLIENT_BLOCK.search(text or "")
    if m and not fields.get("buyer"):
        fields["buyer"] = clean_block(m.group('blk'))

    # ids
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
