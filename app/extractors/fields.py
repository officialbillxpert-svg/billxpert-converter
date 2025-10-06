from __future__ import annotations
import re

from .patterns import (
    FACTURE_NO_RE, INVOICE_NUM_RE, NUM_RE, DATE_RE,
    TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_AMOUNT_NEAR_RE, EUR_STRICT_RE,
    SELLER_BLOCK, CLIENT_BLOCK, EMETTEUR_BLOCK, DESTINATAIRE_BLOCK,
    TVA_RE, SIRET_RE, SIREN_RE, IBAN_RE,
)
from .utils_amounts import _norm_amount, _clean_block

# ---------- Normalisations OCR utiles ----------
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

# ---------- Garde-fous pour blocs parties ----------
STOP_WORDS_RX = re.compile(
    r'\b('
    r'Description|Désignation|Designations?|Items?|Lines?|'
    r'Prix\s*Unitaire|Unit\s*Price|Quantité|Qty|Montant|Amount|'
    r'Total(?:\s*HT|\s*TTC)?|Subtotal|Grand\s*total|TVA|VAT|'
    r'R[èe]glement|Payment|Conditions?|Terms|'
    r'file://|https?://'
    r')\b', re.I
)
HEADER_NOISE_RX = re.compile(r'^\s*(FACTURE|INVOICE)\b', re.I)
DATE_LINE_RX    = re.compile(r'^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{1,2}:\d{2})?\s*$')
ALL_NUMERIC_RX  = re.compile(r'^[\d\s./:-]+$')

def _clip_at_stoppers(block: str) -> str:
    """Coupe un bloc avant mots-clés de table/totaux et à la 1re double-ligne."""
    if not block:
        return ""
    para = block.split("\n\n", 1)[0]
    m = STOP_WORDS_RX.search(para)
    if m:
        para = para[:m.start()]
    return para.strip()

def _sanitize_party_block(block: str, max_lines: int = 6, min_len: int = 5) -> str:
    """Nettoie un bloc Partie: retire lignes vides/bruit, limite la taille, rejette faux positifs."""
    if not block:
        return ""
    lines = [l.strip() for l in block.splitlines()]
    out = []
    for l in lines:
        if not l:
            break  # s’arrête au premier saut de paragraphe
        if HEADER_NOISE_RX.match(l):  # “FACTURE”, “INVOICE”
            continue
        if DATE_LINE_RX.match(l):     # ligne qui n’est qu’une date
            continue
        if ALL_NUMERIC_RX.match(l):   # lignes quasi numériques
            continue
        out.append(l)
        if len(out) >= max_lines:
            break
    s = "\n".join(out).strip()
    if len(s) < min_len:
        return ""
    return s

def _extract_after_label(text: str, labels: list[str]) -> str:
    """
    Lit juste après le label (ex: 'ÉMETTEUR:') et renvoie un petit bloc (nom+adresse) nettoyé.
    On coupe très tôt pour ne pas avaler la table.
    """
    t = text or ""
    for lab in labels:
        m = re.search(rf'(?:^|\n)\s*{lab}\s*:?\s*(.*)', t, re.I)
        if not m:
            continue
        tail = m.group(1)
        tail = "\n".join(tail.splitlines()[:10])  # garde ~10 lignes max
        tail = _clip_at_stoppers(tail)
        tail = _sanitize_party_block(tail)
        if tail:
            return tail
    return ""

def _first_nonempty_lines(block: str, max_lines: int = 5) -> str:
    """Garde les premières lignes non vides d’un bloc pour éviter de capturer trop."""
    if not block:
        return ""
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    return "\n".join(lines[:max_lines]).strip()

def _extract_parties_from_text(text: str) -> tuple[str | None, str | None]:
    """
    (seller, buyer) par ordre:
      1) blocs regex (SELLER_BLOCK/CLIENT_BLOCK…)
      2) fallback ancré (après labels)
      3) heuristique tête de document pour seller
    Le tout avec clipping/sanitizing pour éviter d’avaler la table.
    """
    seller = None
    buyer  = None

    # 1) Blocs explicites
    m = SELLER_BLOCK.search(text or "") or EMETTEUR_BLOCK.search(text or "")
    if m:
        seller = _sanitize_party_block(_clip_at_stoppers(_clean_block(m.group("blk"))))
    m = CLIENT_BLOCK.search(text or "") or DESTINATAIRE_BLOCK.search(text or "")
    if m:
        buyer = _sanitize_party_block(_clip_at_stoppers(_clean_block(m.group("blk"))))

    # 2) Fallback ancré si manquant ou contaminé par des stop-words
    if not seller or STOP_WORDS_RX.search(seller or ""):
        s = _extract_after_label(text, ["ÉMETTEUR", "EMETTEUR", "Seller", "From", "Issuer", "Entreprise", "Company"])
        if s:
            seller = s
    if not buyer or STOP_WORDS_RX.search(buyer or ""):
        b = _extract_after_label(text, ["DESTINATAIRE", "Client", "Buyer", "Bill\s*to", "Invoice\s*to", "Ship\s*to"])
        if b:
            buyer = b

    # 3) Heuristique tête de document pour seller (si toujours vide)
    if not seller:
        head = "\n".join((text or "").splitlines()[:15])
        head = re.sub(r"file://[^\n]+", "", head, flags=re.I)
        head = _clip_at_stoppers(_clean_block(head.split("\n\n")[0]))
        head = _sanitize_party_block(head, max_lines=6)
        if head:
            seller = head

    return (seller or None), (buyer or None)

# ---------- Remplissage principal ----------

def _fill_fields_from_text(result: dict, text: str) -> None:
    text = _normalize_ocr_text(text or "")
    fields = result["fields"]

    # Numéro facture
    m_num = FACTURE_NO_RE.search(text) or INVOICE_NUM_RE.search(text) or NUM_RE.search(text)
    if m_num:
        fields["invoice_number"] = m_num.group(1).strip()

    # Date
    m_date = DATE_RE.search(text)
    if m_date:
        try:
            from dateutil import parser as dateparser
            raw = re.sub(r"\s*([\/\-.])\s*", r"\1", m_date.group(1))
            fields["invoice_date"] = dateparser.parse(raw, dayfirst=True).date().isoformat()
        except Exception:
            fields.setdefault("invoice_date", None)

    # Totaux
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

    # Currency
    if re.search(r"\bEUR\b|€", text, re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): fields["currency"] = "USD"

    # Parties (seller/buyer)
    if not fields.get("seller") or not fields.get("buyer"):
        s, b = _extract_parties_from_text(text)
        if s and not fields.get("seller"):
            fields["seller"] = s
        if b and not fields.get("buyer"):
            fields["buyer"] = b

    # IDs FR
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
