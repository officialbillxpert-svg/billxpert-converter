# app/extractors/fields.py
from __future__ import annotations
import re

# Import the patterns module, then read regexes from it.
# This lets us safely handle optional patterns with getattr().
from . import patterns as P
from .utils_amounts import _norm_amount, _clean_block


def _fill_fields_from_text(result: dict, text: str) -> None:
    fields = result["fields"]

    # ---------- Numéro de facture ----------
    m_num = P.INVOICE_NUM_RE.search(text or "") if hasattr(P, "INVOICE_NUM_RE") else None
    if not m_num and hasattr(P, "NUM_RE"):
        m_num = P.NUM_RE.search(text or "")
    fields["invoice_number"] = m_num.group(1).strip() if m_num else None

    # ---------- Date ----------
    m_date = P.DATE_RE.search(text or "") if hasattr(P, "DATE_RE") else None
    if m_date:
        try:
            from dateutil import parser as dateparser
            raw = re.sub(r'\s*([\/\-.])\s*', r'\1', m_date.group(1))
            fields["invoice_date"] = dateparser.parse(raw, dayfirst=True).date().isoformat()
        except Exception:
            fields["invoice_date"] = None

    # ---------- Totaux ----------
    total_ttc = None
    if hasattr(P, "TOTAL_TTC_NEAR_RE"):
        near = P.TOTAL_TTC_NEAR_RE.findall(text or "")
        if near:
            total_ttc = _norm_amount(near[-1])
    if total_ttc is None and hasattr(P, "EUR_STRICT_RE"):
        m_strict = P.EUR_STRICT_RE.findall(text or "")
        if m_strict:
            total_ttc = _norm_amount(m_strict[-1])
    fields["total_ttc"] = total_ttc

    if hasattr(P, "TOTAL_HT_NEAR_RE"):
        m_ht = P.TOTAL_HT_NEAR_RE.search(text or "")
        if m_ht:
            ht_val = _norm_amount(m_ht.group(1))
            if ht_val is not None:
                fields["total_ht"] = ht_val

    if hasattr(P, "TVA_NEAR_RE"):
        m_tva = P.TVA_NEAR_RE.search(text or "")
        if m_tva:
            tva_val = _norm_amount(m_tva.group(1))
            if tva_val is not None:
                fields["total_tva"] = tva_val

    # ---------- Devise ----------
    if re.search(r"\bEUR\b|€", text, re.I):
        fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I):
        fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I):
        fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I):
        fields["currency"] = "USD"

    # ---------- Seller / Buyer (blocs) ----------
    if hasattr(P, "SELLER_BLOCK"):
        m = P.SELLER_BLOCK.search(text or "")
        if m and not fields.get("seller"):
            fields["seller"] = _clean_block(m.group("blk"))

    if hasattr(P, "CLIENT_BLOCK"):
        m = P.CLIENT_BLOCK.search(text or "")
        if m and not fields.get("buyer"):
            fields["buyer"] = _clean_block(m.group("blk"))

    # Optionnels : Émetteur / Destinataire
    EMETTEUR_BLOCK = getattr(P, "EMETTEUR_BLOCK", None)
    if EMETTEUR_BLOCK:
        m = EMETTEUR_BLOCK.search(text or "")
        if m and not fields.get("seller"):
            fields["seller"] = _clean_block(m.group("blk"))

    DESTINATAIRE_BLOCK = getattr(P, "DESTINATAIRE_BLOCK", None)
    if DESTINATAIRE_BLOCK:
        m = DESTINATAIRE_BLOCK.search(text or "")
        if m and not fields.get("buyer"):
            fields["buyer"] = _clean_block(m.group("blk"))

    # ---------- Identifiants FR ----------
    if hasattr(P, "TVA_RE"):
        m = P.TVA_RE.search(text or "")
        if m and not fields.get("seller_tva"):
            fields["seller_tva"] = m.group(0).replace(" ", "")

    # SIRET or fallback SIREN
    if hasattr(P, "SIRET_RE"):
        m = P.SIRET_RE.search(text or "")
    else:
        m = None
    if m and not fields.get("seller_siret"):
        fields["seller_siret"] = m.group(0)
    elif not fields.get("seller_siret") and hasattr(P, "SIREN_RE"):
        m2 = P.SIREN_RE.search(text or "")
        if m2:
            fields["seller_siret"] = m2.group(0)

    if hasattr(P, "IBAN_RE"):
        m = P.IBAN_RE.search(text or "")
        if m and not fields.get("seller_iban"):
            fields["seller_iban"] = m.group(0).replace(" ", "")
