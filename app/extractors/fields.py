from __future__ import annotations
import re

from .patterns import (
    FACTURE_NO_RE, INVOICE_NUM_RE, NUM_RE, DATE_RE,
    TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_AMOUNT_NEAR_RE, EUR_STRICT_RE,
    SELLER_BLOCK, CLIENT_BLOCK, EMETTEUR_BLOCK, DESTINATAIRE_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE,
)
from .utils_amounts import _norm_amount, _clean_block

# --- Petites normalisations OCR utiles ---
_OCR_SPACES_FIX = [
    ("\u00A0", " "),  # NBSP
    ("  ", " "),
]
_OCR_LABEL_FIX_RX = re.compile(r"(?:ÉMETTEUR|EMETTEUR)\s*[: ]*\s*(DESTINATAIRE)\s*:", re.I)

def _normalize_ocr_text(t: str) -> str:
    if not t:
        return t
    t = _OCR_LABEL_FIX_RX.sub(r"ÉMETTEUR:\n\1:", t)
    for a, b in _OCR_SPACES_FIX:
        t = t.replace(a, b)
    return t


def _first_nonempty_lines(block: str, max_lines: int = 5) -> str:
    """Garde les 3–5 premières lignes non vides d’un bloc pour éviter de capturer trop."""
    if not block:
        return block
    lines = [l.strip() for l in block.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines[:max_lines]).strip()


def _extract_parties_from_text(text: str) -> tuple[str | None, str | None]:
    """
    Essaie plusieurs patterns pour seller/buyer.
    Renvoie (seller, buyer) éventuellement None si non trouvés.
    """
    seller = None
    buyer  = None

    # 1) Blocs explicites
    m = SELLER_BLOCK.search(text or "") or EMETTEUR_BLOCK.search(text or "")
    if m:
        seller = _first_nonempty_lines(_clean_block(m.group("blk")))

    m = CLIENT_BLOCK.search(text or "") or DESTINATAIRE_BLOCK.search(text or "")
    if m:
        buyer = _first_nonempty_lines(_clean_block(m.group("blk")))

    # 2) Si labels absents, heuristique simple : top du document vs zone "Client"
    if not seller:
        # haut de page (premières 15 lignes), souvent l'émetteur
        head = "\n".join((text or "").splitlines()[:15])
        # on évite les métadonnées type "file://"
        head = re.sub(r"file://[^\n]+", "", head, flags=re.I)
        # On coupe à la première double-ligne pour éviter de tout avaler
        head = head.split("\n\n")[0]
        head = _clean_block(head)
        if head and len(head) > 8:
            seller = _first_nonempty_lines(head, max_lines=6)

    if not buyer:
        # heuristique: chercher "Client" ou "Buyer" + bloc suivant
        m = re.search(r'(?:^|\n)\s*(Client|Buyer)\s*:?\s*(.+?)(?:\n{2,}|\Z)', text or "", re.I | re.S)
        if m:
            buyer = _first_nonempty_lines(_clean_block(m.group(2)), max_lines=6)

    return (seller or None), (buyer or None)


def _fill_fields_from_text(result: dict, text: str) -> None:
    text = _normalize_ocr_text(text or "")
    fields = result["fields"]

    # ---- Numéro facture
    m_num = FACTURE_NO_RE.search(text) or INVOICE_NUM_RE.search(text) or NUM_RE.search(text)
    if m_num:
        fields["invoice_number"] = m_num.group(1).strip()

    # ---- Date
    m_date = DATE_RE.search(text)
    if m_date:
        try:
            from dateutil import parser as dateparser
            raw = re.sub(r"\s*([\/\-.])\s*", r"\1", m_date.group(1))
            fields["invoice_date"] = dateparser.parse(raw, dayfirst=True).date().isoformat()
        except Exception:
            fields.setdefault("invoice_date", None)

    # ---- Totaux
    total_ttc = None
    near = TOTAL_TTC_NEAR_RE.findall(text)
    if near:
        total_ttc = _norm_amount(near[-1])

    if total_ttc is None:
        m_strict = EUR_STRICT_RE.findall(text)
        if m_strict:
            total_ttc = _norm_amount(m_strict[-1])

    if total_ttc is not None:
        fields["total_ttc"] = total_ttc

    m_ht = TOTAL_HT_NEAR_RE.search(text)
    if m_ht:
        ht_val = _norm_amount(m_ht.group(1))
        if ht_val is not None:
            fields["total_ht"] = ht_val

    m_tva_amt = TVA_AMOUNT_NEAR_RE.search(text)
    if m_tva_amt:
        tva_val = _norm_amount(m_tva_amt.group(1))
        if tva_val is not None:
            fields["total_tva"] = tva_val

    # ---- Currency
    if re.search(r"\bEUR\b|€", text, re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): fields["currency"] = "USD"

    # ---- Parties (seller/buyer)
    if not fields.get("seller") or not fields.get("buyer"):
        s, b = _extract_parties_from_text(text)
        if s and not fields.get("seller"):
            fields["seller"] = s
        if b and not fields.get("buyer"):
            fields["buyer"] = b

    # ---- IDs FR (dans l’ordre de priorité)
    if not fields.get("seller_tva"):
        m = TVA_RE.search(text)
        if m: fields["seller_tva"] = m.group(0).replace(" ", "")
    if not fields.get("seller_siret"):
        m = SIRET_RE.search(text)
        if m:
            fields["seller_siret"] = m.group(0)
        else:
            m2 = SIREN_RE.search(text)
            if m2:
                fields["seller_siret"] = m2.group(0)
    if not fields.get("seller_iban"):
        m = IBAN_RE.search(text)
        if m:
            fields["seller_iban"] = m.group(0).replace(" ", "")
