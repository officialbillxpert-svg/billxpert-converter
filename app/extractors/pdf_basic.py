# app/extractors/pdf_basic.py
from __future__ import annotations

from pdfminer.high_level import extract_text
from dateutil import parser as dateparser
from pathlib import Path
import re
from typing import Optional, Dict, Any, List, Tuple

# --- import optionnel de pdfplumber (si installé sur l'instance) ---
try:
    import pdfplumber  # type: ignore
except Exception:
    pdfplumber = None

# =============== Regex de base ===============

# numéro/date
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

# totaux (HT / TVA / TTC)
TOTAL_TTC_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Total\s*amount|Grand\s*total|Total\s*à\s*payer)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
TOTAL_HT_RE = re.compile(
    r'(?:Total\s*HT)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
TVA_AMOUNT_RE = re.compile(
    r'(?:TVA(?:\s*\([^)]+\))?)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)

# toute somme "euros" générique
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2}))\s*€?')

# Identifiants FR
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
# IBAN FR avec ou sans espaces (FR + 2 chiffres + blocs de chiffres)
IBAN_RE  = re.compile(r'\bFR\d{2}(?:[ ]?\d{4}){5}\b', re.I)
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')

# Blocs parties
SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)

# Détection du titre d'une ligne article (fallback texte)
# Ex: "PREST-001 — Développement"
LINE_TITLE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s*$',
    re.M
)

# Quantité sur une ligne seule (extraits en colonne)
QTY_LINE_RX = re.compile(r'^\d{1,4}$', re.M)

# Taux TVA mentionné (20 / 10 / 5,5)
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

# Hints d'entêtes pour pdfplumber
TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

# =============== Helpers ===============

def _norm_amount(s: str | None) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    s = s.replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None

def _clean_block(s: str | None) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None

def _approx(a: Optional[float], b: Optional[float], tol: float = 1.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Complète HT/TVA/TTC si on connaît un taux (ex: 20 -> 0.20)."""
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0

    ht, tva, ttc = total_ht, total_tva, total_ttc

    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht is None:  ht = ht_calc
            if tva is None: tva = tva_calc
        except Exception:
            pass

    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception:
            pass

    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc

# ---------- pdfplumber helpers ----------
def _norm_header_cell(s: str) -> str:
    s = (s or "").strip().lower()
    s = (s.replace("é","e").replace("è","e").replace("ê","e")
           .replace("à","a").replace("û","u").replace("ï","i"))
    s = s.replace("\n"," ").replace("\t"," ")
    s = re.sub(r"\s+"," ", s)
    return s

def _map_header_indices(headers: List[str]) -> Optional[Dict[str, int]]:
    idx: Dict[str, Optional[int]] = {}
    norm = [_norm_header_cell(h) for h in headers]

    def match_one(*cands):
        for i, h in enumerate(norm):
            for c in cands:
                if c in h:
                    return i
        return None

    idx["ref"]    = match_one(*TABLE_HEADER_HINTS[0])
    idx["label"]  = match_one(*TABLE_HEADER_HINTS[1])
    idx["qty"]    = match_one(*TABLE_HEADER_HINTS[2])
    idx["unit"]   = match_one(*TABLE_HEADER_HINTS[3])
    idx["amount"] = match_one(*TABLE_HEADER_HINTS[4])

    if all(v is None for v in idx.values()):
        return None
    return {k: v for k, v in idx.items() if v is not None}

def _parse_lines_with_pdfplumber(pdf_path: str) -> List[Dict[str, Any]]:
    """Tente d’extraire un tableau d’articles avec pdfplumber. Fallback [] si lib absente/échec."""
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = []
                t = page.extract_table()
                if t: tables.append(t)
                t2 = page.extract_table({"vertical_strategy":"lines", "horizontal_strategy":"lines"})
                if t2: tables.append(t2)

                for tbl in tables:
                    tbl = [[(c or "").strip() for c in (row or [])] for row in (tbl or []) if any((row or []))]
                    if not tbl or len(tbl) < 2:
                        continue

                    header = tbl[0]
                    idx = _map_header_indices(header)
                    if not idx:
                        continue

                    for line in tbl[1:]:
                        def get(i):
                            return line[i] if (i is not None and i < len(line)) else ""
                        ref   = get(idx.get("ref"))
                        label = get(idx.get("label")) or ref
                        qty_s = get(idx.get("qty"))
                        pu_s  = get(idx.get("unit"))
                        amt_s = get(idx.get("amount"))

                        try:
                            qty = int(re.sub(r"[^\d]", "", qty_s)) if qty_s else None
                        except Exception:
                            qty = None

                        pu_f  = _norm_amount(pu_s)
                        amt_f = _norm_amount(amt_s)

                        if not (label or pu_f is not None or amt_f is not None):
                            continue

                        rows.append({
                            "ref":        (ref or "").strip() or None,
                            "label":      (label or "").strip(),
                            "qty":        qty,
                            "unit_price": pu_f,
                            "amount":     amt_f
                        })
        # dédoublonnage
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        return uniq
    except Exception:
        return []

# ---------- Fallback « bloc » quand les colonnes sont sur des lignes séparées ----------
def _parse_lines_blockwise(text: str) -> List[Dict[str, Any]]:
    """
    1) Récupère les items par leur titre: 'REF — Libellé'
    2) Récupère toutes les quantités présentes sur des lignes seules
    3) Récupère toutes les valeurs monétaires, en filtrant les totaux (HT/TVA/TTC)
    4) Associe qty + (pu,amount) séquentiellement aux items
    """
    items = []
    for m in LINE_TITLE_RX.finditer(text):
        items.append({"ref": m.group("ref"), "label": m.group("label").strip()})

    if not items:
        return []

    # Quantités en colonne
    qtys = [int(q) for q in QTY_LINE_RX.findall(text)]

    # Toutes les valeurs € du document
    euros_all = [ _norm_amount(x) for x in EUR_RE.findall(text) ]
    euros_all = [x for x in euros_all if x is not None]

    # Essayer d’identifier les totaux pour les retirer du pool de montants lignes
    totals_to_exclude: List[float] = []
    m_ht = TOTAL_HT_RE.search(text)
    if m_ht: 
        v = _norm_amount(m_ht.group(1))
        if v is not None: totals_to_exclude.append(v)
    m_tv = TVA_AMOUNT_RE.search(text)
    if m_tv:
        v = _norm_amount(m_tv.group(1))
        if v is not None: totals_to_exclude.append(v)
    m_tc = TOTAL_TTC_RE.search(text)
    if m_tc:
        v = _norm_amount(m_tc.group(1))
        if v is not None: totals_to_exclude.append(v)

    euros = []
    for v in euros_all:
        # retire les totaux identifiés (tolérance)
        if any(_approx(v, t, 0.5) for t in totals_to_exclude):
            continue
        euros.append(v)

    # Heuristique d'association :
    # - idéalement on a 2 * n_items valeurs (PU, MONTANT) pour chaque ligne.
    # - sinon, si on a n_items valeurs, on considère que ce sont des montants TTC/HT et on calcule PU via qty.
    n = len(items)
    rows: List[Dict[str, Any]] = []
    if len(euros) >= 2 * n:
        # on prend les 2*n premières pour éviter d'attraper des mentions hors tableau
        euros = euros[:2*n]
        for i, it in enumerate(items):
            qty = qtys[i] if i < len(qtys) else None
            pu  = euros[2*i]
            amt = euros[2*i+1]
            # si qty manquante et pu/amt présents, on laisse qty=None
            rows.append({
                "ref": it["ref"],
                "label": it["label"],
                "qty": qty,
                "unit_price": pu,
                "amount": amt
            })
    elif len(euros) >= n:
        # considérer que ce sont des montants (dernieres valeurs souvent = montants)
        # on prend les n dernières valeurs (plus proches des lignes dans l'ordre visuel)
        amounts = euros[-n:]
        for i, it in enumerate(items):
            qty = qtys[i] if i < len(qtys) else None
            amt = amounts[i] if i < len(amounts) else None
            pu = None
            if qty and qty > 0 and amt is not None:
                pu = round(amt / qty, 2)
            rows.append({
                "ref": it["ref"],
                "label": it["label"],
                "qty": qty,
                "unit_price": pu,
                "amount": amt
            })
    else:
        # on n’a pas assez d’€ -> retourner titres + qty
        for i, it in enumerate(items):
            rows.append({
                "ref": it["ref"],
                "label": it["label"],
                "qty": qtys[i] if i < len(qtys) else None,
                "unit_price": None,
                "amount": None
            })

    return rows

# =============== Extraction principale ===============
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = extract_text(p) or ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # --- Champs simples ---
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    # Totaux individuels
    total_ht  = _norm_amount(TOTAL_HT_RE.search(text).group(1))  if TOTAL_HT_RE.search(text) else None
    total_tva = _norm_amount(TVA_AMOUNT_RE.search(text).group(1)) if TVA_AMOUNT_RE.search(text) else None
    total_ttc = _norm_amount(TOTAL_TTC_RE.search(text).group(1)) if TOTAL_TTC_RE.search(text) else None

    # Fallback TTC si rien trouvé: prendre la plus “grosse” somme
    if total_ttc is None:
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)

    # Devise
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"

    # Taux TVA
    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vr = m_vat.group(1)
        vat_rate = '5.5' if vr in ('5,5', '5.5') else vr

    # Résultat de base
    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "total_ht":  total_ht,
            "total_tva": total_tva,
            "total_ttc": total_ttc,
            "currency":  currency or "EUR",
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # Vendeur / Client (blocs)
    m = SELLER_BLOCK.search(text)
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))

    m = CLIENT_BLOCK.search(text)
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

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
            fields["seller_siret"] = m2.group(0)

    m = IBAN_RE.search(text)
    if m and not fields.get("seller_iban"):
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # Lignes d'articles
    lines: List[Dict[str, Any]] = []
    # 1) tableau structuré si pdfplumber dispo
    try:
        lines = _parse_lines_with_pdfplumber(str(p))
    except Exception:
        lines = []
    # 2) fallback bloc si rien
    if not lines:
        lines = _parse_lines_blockwise(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        # Si totaux manquants, essayer d'inférer
        # somme des montants de lignes (qu'on suppose HT en général)
        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None

        if fields.get("total_ht") is None and sum_lines:
            fields["total_ht"] = sum_lines

        ht, tv, tt = _infer_totals(fields.get("total_ttc"), fields.get("total_ht"), fields.get("total_tva"), vat_rate)
        if ht is not None: fields["total_ht"]  = ht
        if tv is not None: fields["total_tva"] = tv
        if tt is not None: fields["total_ttc"] = tt

    return result
