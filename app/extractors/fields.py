import re
from .patterns import (
    INVOICE_NUM_RE, NUM_RE, DATE_RE,
    TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_NEAR_RE,
    EUR_STRICT_RE, SELLER_BLOCK, CLIENT_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE,
    EMETTEUR_BLOCK, DESTINATAIRE_BLOCK
)
from .utils_amounts import _norm_amount, _clean_block

def _fill_fields_from_text(result: dict, text: str) -> None:
    fields = result["fields"]

    # Numéro facture
    m_num = INVOICE_NUM_RE.search(text or "")
    if not m_num:
        m_num = NUM_RE.search(text or "")
    fields["invoice_number"] = m_num.group(1).strip() if m_num else None

    # Date
    m_date = DATE_RE.search(text or "")
    if m_date:
        try:
            from dateutil import parser as dateparser
            raw = re.sub(r'\s*([\/\-.])\s*', r'\1', m_date.group(1))
            fields["invoice_date"] = dateparser.parse(raw, dayfirst=True).date().isoformat()
        except Exception:
            fields["invoice_date"] = None

    # Totaux
    total_ttc = None
    near = TOTAL_TTC_NEAR_RE.findall(text or "")
    if near:
        total_ttc = _norm_amount(near[-1])
    if total_ttc is None:
        m_strict = EUR_STRICT_RE.findall(text or "")
        if m_strict:
            total_ttc = _norm_amount(m_strict[-1])
    fields["total_ttc"] = total_ttc

    m_ht = TOTAL_HT_NEAR_RE.search(text or "")
    if m_ht:
        ht_val = _norm_amount(m_ht.group(1))
        if ht_val is not None:
            fields["total_ht"] = ht_val

    m_tva = TVA_NEAR_RE.search(text or "")
    if m_tva:
        tva_val = _norm_amount(m_tva.group(1))
        if tva_val is not None:
            fields["total_tva"] = tva_val

    # Currency
    if re.search(r"\bEUR\b|€", text, re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): fields["currency"] = "USD"

    # Seller / Buyer
    m = SELLER_BLOCK.search(text or "")
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))

    m = CLIENT_BLOCK.search(text or "")
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

    # Nouveaux blocs (Émetteur / Destinataire)
    m = EMETTEUR_BLOCK.search(text or "")
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))

    m = DESTINATAIRE_BLOCK.search(text or "")
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

    # Identifiants FR
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
