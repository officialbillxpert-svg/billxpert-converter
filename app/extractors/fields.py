
from __future__ import annotations
import re
from typing import Dict, Any, Tuple, Optional
from .patterns import (
    FACTURE_NO_RE, INVOICE_NUM_RE, DATE_RE, EUR_STRICT_RE,
    TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_AMOUNT_NEAR_RE,
    SELLER_BLOCK, CLIENT_BLOCK, EMETTEUR_BLOCK, DESTINATAIRE_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE
)
from .utils_amounts import _norm_amount, _clean_block

def _first_group(m: Optional[re.Match]) -> Optional[str]:
    return m.group(1).strip() if m else None

def _extract_invoice_number(text: str) -> Optional[str]:
    return _first_group(FACTURE_NO_RE.search(text)) or _first_group(INVOICE_NUM_RE.search(text))

def _extract_invoice_date(text: str) -> Optional[str]:
    return _first_group(DATE_RE.search(text))

def _extract_totals(text: str) -> Dict[str, Any]:
    total_ttc = total_ht = total_tva = None
    # proximity: look at lines
    for line in text.splitlines():
        low = line.lower()
        if TOTAL_TTC_NEAR_RE.search(low):
            m = EUR_STRICT_RE.search(line)
            if m: total_ttc = _norm_amount(m.group(0))
        elif TOTAL_HT_NEAR_RE.search(low):
            m = EUR_STRICT_RE.search(line)
            if m: total_ht = _norm_amount(m.group(0))
        elif TVA_AMOUNT_NEAR_RE.search(low):
            m = EUR_STRICT_RE.search(line)
            if m: total_tva = _norm_amount(m.group(0))
    return {"total_ht": total_ht, "total_tva": total_tva, "total_ttc": total_ttc}

def _extract_parties(text: str) -> Tuple[Optional[str], Optional[str]]:
    seller = (_first_group(SELLER_BLOCK.search(text)) or
              _first_group(EMETTEUR_BLOCK.search(text)))
    buyer = (_first_group(CLIENT_BLOCK.search(text)) or
             _first_group(DESTINATAIRE_BLOCK.search(text)))
    return _clean_block(seller), _clean_block(buyer)

def _fill_fields_from_text(text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    fields["invoice_number"] = _extract_invoice_number(text)
    fields["invoice_date"]   = _extract_invoice_date(text)
    fields.update(_extract_totals(text))
    seller, buyer = _extract_parties(text)
    if seller: fields["seller"] = seller
    if buyer:  fields["buyer"] = buyer

    # extra ids
    tva = _first_group(TVA_RE.search(text))
    if tva: fields["seller_vat"] = tva
    siret = _first_group(SIRET_RE.search(text))
    if siret: fields["seller_siret"] = siret
    siren = _first_group(SIREN_RE.search(text))
    if siren: fields["seller_siren"] = siren
    iban = _first_group(IBAN_RE.search(text))
    if iban: fields["iban"] = iban
    return fields
