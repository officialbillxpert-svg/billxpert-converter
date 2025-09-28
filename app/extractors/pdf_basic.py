# app/extractors/pdf_basic.py
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# --- Dépendances PDF ---
# pdfminer.six pour le texte "linéaire"
from pdfminer.high_level import extract_text as _extract_text

# pdfplumber (optionnel) pour tables + mots avec positions (bbox)
try:
    import pdfplumber  # type: ignore
except Exception:
    pdfplumber = None

# date parsing
from dateutil import parser as dateparser


# =============================================================================
#                               REGEX & CONSTANTES
# =============================================================================

# Numéro facture / date
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/\.]{2,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

# Totaux (multilingue / multi-orthographes)
TOTAL_TTC_RE = re.compile(
    r'(?:Total\s*(?:TTC|à\s*payer|amount\s*due)|Grand\s*total|Total\s*general)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
TOTAL_HT_RE = re.compile(
    r'(?:Total\s*HT|Sous-?total|Subtotal)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
TOTAL_TVA_RE = re.compile(
    r'(?:TVA|VAT|Tax(?:es)?)\s*(?:\(\s*\d{1,2}(?:[.,]\d)?\s*%\s*\))?\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(\d{1,2}(?:[.,]\d)?)\s*%?', re.I)

# Nombre monétaire générique
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_ID_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\b[A-Z]{2}\d{2}(?:\s?\w{4}){3}\s?(?:\w{4}\s?\w{3}\s?\w{5}|\w{11})\b')

# Blocs parties — ancres étendues
SELLER_ANCHORS = [
    "émetteur", "vendeur", "seller", "vendor", "supplier", "from", "expéditeur"
]
BUYER_ANCHORS = [
    "client", "acheteur", "buyer", "bill to", "billed to", "facturé à", "adresse de facturation", "sold to", "ship to"
]

# Lignes d’articles — fallback texte (ex : "REF — Libellé ...  12  10,00 €  120,00 €")
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Entêtes de colonnes (pour pdfplumber.extract_table)
TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code", "item"),
    ("désignation", "designation", "libellé", "description", "label", "designation/description"),
    ("qté", "qte", "qty", "quantité", "quantity"),
    ("pu", "prix unitaire", "unit price", "price"),
    ("montant", "total", "amount", "line total", "total ht", "montant ht", "montant ttc"),
]


# =============================================================================
#                                   HELPERS
# =============================================================================

def _norm_amount(s: str | None) -> Optional[float]:
    """Normalise les nombres '1 234,56' / '1.234,56' / '1234.56' -> 1234.56."""
    if not s:
        return None
    s = s.strip().replace('\xa0', ' ').replace(' ', '')
    if ',' in s and '.' in s:
        # supposer . = milliers, , = décimales
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None


def _approx(a: Optional[float], b: Optional[float], tol: float = 1.5) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Complète HT/TVA/TTC si on connaît un taux (ex: 20 -> 0.20)."""
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    try:
        rate = float(str(vat_rate).replace(',', '.')) / 100.0
    except Exception:
        return total_ht, total_tva, total_ttc

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


def _norm_header_cell(s: str) -> str:
    s = (s or "").strip().lower()
    repl = (
        ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
        ("à", "a"), ("â", "a"),
        ("î", "i"), ("ï", "i"),
        ("ô", "o"),
        ("û", "u"), ("ü", "u"),
    )
    for a, b in repl:
        s = s.replace(a, b)
    s = s.replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _map_header_indices(headers: List[str]) -> Optional[Dict[str, int]]:
    idx: Dict[str, Optional[int]] = {}
    norm = [_norm_header_cell(h) for h in headers]

    def match_one(cands: Tuple[str, ...]) -> Optional[int]:
        for i, h in enumerate(norm):
            for c in cands:
                if c in h:
                    return i
        return None

    idx["ref"]    = match_one(TABLE_HEADER_HINTS[0])
    idx["label"]  = match_one(TABLE_HEADER_HINTS[1])
    idx["qty"]    = match_one(TABLE_HEADER_HINTS[2])
    idx["unit"]   = match_one(TABLE_HEADER_HINTS[3])
    idx["amount"] = match_one(TABLE_HEADER_HINTS[4])

    if all(v is None for v in idx.values()):
        return None
    return {k: v for k, v in idx.items() if v is not None}


def _clean_block(s: str) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None


def _extract_block_by_anchors(text: str, anchors: List[str], stop_anchors: List[str]) -> Optional[str]:
    """
    Prend un bloc débutant par une ancre (ex: 'Client', 'Bill to'...) jusqu'à la
    prochaine ancre connue ou un double saut de ligne.
    """
    # Construire regex d'ancre et d'arrêt
    a = r'|'.join([re.escape(x) for x in anchors])
    b = r'|'.join([re.escape(x) for x in stop_anchors])
    rx = re.compile(rf'(?P<a>{a})\s*:?\s*(?P<blk>.+?)(?:\n{{2,}}|{b}\b)', re.I | re.S)
    m = rx.search(text)
    if m:
        return _clean_block(m.group('blk'))
    return None


def _parse_lines_fallback_text(text: str) -> List[Dict[str, Any]]:
    """
    Fallback simple basé sur LINE_RX (quand la facture est très “propre” dans le flux texte).
    """
    rows: List[Dict[str, Any]] = []
    for m in LINE_RX.finditer(text):
        qty = int(m.group('qty'))
        pu  = _norm_amount(m.group('pu'))
        amt = _norm_amount(m.group('amt'))
        rows.append({
            "ref":        m.group('ref'),
            "label":      m.group('label').strip(),
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    return rows


def _parse_lines_with_pdfplumber_tables(pdf_path: str) -> List[Dict[str, Any]]:
    """
    1er essai : extraire un vrai tableau avec pdfplumber.extract_table(s) et mapper les entêtes.
    """
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = []
                t = page.extract_table()
                if t: tables.append(t)
                # Essai avec stratégie lignes
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
                        def get(i: Optional[int]) -> str:
                            return line[i] if (i is not None and i < len(line)) else ""

                        ref   = get(idx.get("ref"))
                        label = get(idx.get("label")) or ref
                        qty_s = get(idx.get("qty"))
                        pu_s  = get(idx.get("unit"))
                        amt_s = get(idx.get("amount"))

                        qty: Optional[int] = None
                        try:
                            q = re.sub(r"[^\d]", "", qty_s or "")
                            qty = int(q) if q else None
                        except Exception:
                            qty = None

                        pu_f  = _norm_amount(pu_s)
                        amt_f = _norm_amount(amt_s)

                        # ignorer lignes clairement vides
                        if not (label or pu_f is not None or amt_f is not None or qty is not None):
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


def _extract_lines_by_words(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Fallback 2 : si les tableaux ne sortent pas proprement, on reconstruit des lignes
    en regroupant les mots par “ligne Y” et en cherchant qty + 1 ou 2 montants.
    Heuristique robuste pour beaucoup de factures simples.
    """
    if pdfplumber is None:
        return []

    def find_prices(s: str) -> List[float]:
        vals = []
        for m in re.finditer(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)\s*€?', s):
            v = _norm_amount(m.group(1))
            if v is not None:
                vals.append(v)
        return vals

    def guess_ref_and_label(s: str) -> Tuple[Optional[str], str]:
        # Cherche un token style "PREST-001" / "ART123" / "AB-12"
        m = re.search(r'\b([A-Z]{2,}[A-Z0-9]*[-_]\d{1,4}|[A-Z]{2,}\d{2,})\b', s)
        ref = m.group(1) if m else None
        if ref:
            label = (s.replace(ref, '')).strip(' -—–:;·')
        else:
            label = s.strip()
        return ref, label

    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words(
                    keep_blank_chars=False, use_text_flow=True, horizontal_ltr=True
                )
                if not words:
                    continue
                # Regroupe les mots par "ligne" via y (tolérance)
                lines_map: Dict[int, List[dict]] = {}
                for w in words:
                    # y moyen
                    y = int(round((w['top'] + w['bottom']) / 2.0))
                    lines_map.setdefault(y, []).append(w)

                for y in sorted(lines_map.keys()):
                    line_words = sorted(lines_map[y], key=lambda w: w['x0'])
                    text_line = " ".join(w['text'] for w in line_words).strip()
                    if not text_line:
                        continue

                    # Cherche au moins une quantité claire (petit entier)
                    qty: Optional[int] = None
                    mqty = re.search(r'\b(\d{1,3})\b', text_line)
                    if mqty:
                        try:
                            q = int(mqty.group(1))
                            if 0 < q < 1000:
                                qty = q
                        except Exception:
                            qty = None

                    prices = find_prices(text_line)
                    pu_f: Optional[float] = None
                    amt_f: Optional[float] = None
                    if len(prices) >= 2:
                        # Heuristique : dernier = Montant, précédent = PU
                        amt_f = prices[-1]
                        pu_f  = prices[-2]
                    elif len(prices) == 1:
                        # Un seul prix : on le prend comme Montant
                        amt_f = prices[0]

                    # On filtre les lignes qui sont probablement des "Total"
                    if re.search(r'\b(total|tva|subtotal|grand total|amount due)\b', text_line, re.I):
                        continue
                    # Et celles qui sont purement “coordonnées”
                    if re.search(r'\b(SIRET|TVA|IBAN|FR\d{2}\s?\w{4})\b', text_line, re.I):
                        continue

                    # Construire ref/label
                    ref, label = guess_ref_and_label(text_line)

                    # Heuristique pour ignorer les titres de colonnes
                    if re.search(r'\b(qte|qty|quantite|pu|prix|montant|total)\b', text_line, re.I):
                        continue

                    # On garde les lignes qui ont au moins label + (qty ou un prix)
                    if label and (qty is not None or pu_f is not None or amt_f is not None):
                        rows.append({
                            "ref":        ref,
                            "label":      label,
                            "qty":        qty,
                            "unit_price": pu_f,
                            "amount":     amt_f
                        })

        # Nettoyage / dédoublonnage léger par (label, qty, amount)
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("label"), r.get("qty"), r.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        return uniq
    except Exception:
        return []


# =============================================================================
#                               EXTRACTION PRINCIPALE
# =============================================================================

def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)
    text = _extract_text(p) or ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # ---------------- Numéro / Date ----------------
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    # ---------------- Totaux ----------------
    total_ttc = None
    m = TOTAL_TTC_RE.search(text)
    if m:
        total_ttc = _norm_amount(m.group(1))
    if total_ttc is None:
        # fallback: chercher la plus grosse somme plausible
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)

    total_ht = None
    m = TOTAL_HT_RE.search(text)
    if m:
        total_ht = _norm_amount(m.group(1))

    total_tva = None
    m = TOTAL_TVA_RE.search(text)
    if m:
        total_tva = _norm_amount(m.group(1))

    # Taux TVA
    vat_rate = None
    m = VAT_RATE_RE.search(text)
    if m:
        vr = m.group(1)
        vat_rate = vr.replace(',', '.')
        if vat_rate in ('5,5', '5.5'):
            vat_rate = '5.5'

    # Compléter si possible
    total_ht, total_tva, total_ttc = _infer_totals(total_ttc, total_ht, total_tva, vat_rate)

    # ---------------- Devise ----------------
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"
    else: currency = "EUR"

    # ---------------- Résultat initial ----------------
    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "total_ht":       total_ht,
            "total_tva":      total_tva,
            "total_ttc":      total_ttc,
            "currency":       currency,
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # ---------------- Seller / Buyer ----------------
    seller = _extract_block_by_anchors(text, SELLER_ANCHORS, BUYER_ANCHORS)
    if seller:
        fields["seller"] = seller
    buyer = _extract_block_by_anchors(text, BUYER_ANCHORS, SELLER_ANCHORS)
    if buyer:
        fields["buyer"] = buyer

    # Identifiants FR du vendeur (ou globalement dans le doc, c’est ok)
    m = TVA_ID_RE.search(text)
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
    if m:
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # ---------------- Lignes d'articles ----------------
    lines: List[Dict[str, Any]] = []

    # 1) Essai via tableaux structurés
    try:
        tbl_lines = _parse_lines_with_pdfplumber_tables(str(p))
        if tbl_lines:
            lines = tbl_lines
    except Exception:
        pass

    # 2) Si vides ou si beaucoup de champs manquent, fallback “words by line”
    needs_proximity = (not lines) or all(
        (r.get("unit_price") is None and r.get("amount") is None) for r in lines
    )
    if needs_proximity:
        try:
            prox_lines = _extract_lines_by_words(str(p))
            # Fusionner naïvement : si on a déjà des lignes, on complète PU/Montant si manquants
            if lines and prox_lines:
                # Index rapide par (label, qty) pour matcher
                idx = {}
                for r in prox_lines:
                    key = (r.get("label"), r.get("qty"))
                    idx.setdefault(key, []).append(r)
                for r in lines:
                    key = (r.get("label"), r.get("qty"))
                    if r.get("unit_price") is None or r.get("amount") is None:
                        cands = idx.get(key) or []
                        for c in cands:
                            if r.get("unit_price") is None and c.get("unit_price") is not None:
                                r["unit_price"] = c["unit_price"]
                            if r.get("amount") is None and c.get("amount") is not None:
                                r["amount"] = c["amount"]
            elif prox_lines:
                lines = prox_lines
        except Exception:
            pass

    # 3) Fallback final regex brut si toujours vide
    if not lines:
        lines = _parse_lines_fallback_text(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        # Si HT/TVA/TTC encore incomplets, tenter somme des montants de lignes
        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None
        if sum_lines:
            if total_ttc and _approx(sum_lines, total_ttc):
                # lignes ≈ TTC → recalculer HT/TVA
                th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
                fields["total_ht"]  = th if th is not None else fields.get("total_ht")
                fields["total_tva"] = tv if tv is not None else fields.get("total_tva")
                fields["total_ttc"] = tt if tt is not None else fields.get("total_ttc")
            else:
                # considérer sum_lines = HT si pas d’info contraire
                th, tv, tt = _infer_totals(total_ttc, sum_lines, fields.get("total_tva"), vat_rate)
                if th is not None: fields["total_ht"]  = th
                if tv is not None: fields["total_tva"] = tv
                if tt is not None: fields["total_ttc"] = tt

    return result
