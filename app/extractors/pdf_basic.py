# app/extractors/pdf_basic.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import io
import re

from pdfminer.high_level import extract_text as _extract_text
from dateutil import parser as dateparser

# pdfplumber est optionnel, mais requis pour l’extraction par positions x/y
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# =========================
#          REGEX
# =========================
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')
TOTAL_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Montant\s*TTC|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)

# fallback "une ligne par article" (peu fiable sur ton PDF)
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label", "réf / désignation", "ref / designation"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

# =========================
#        HELPERS
# =========================
def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(" ", "")
    # 1 234,56 / 1.234,56 / 1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    s = s.replace("€", "")
    try:
        return round(float(s), 2)
    except Exception:
        return None

def _clean_block(s: str) -> Optional[str]:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s or None

def _parse_lines_regex(text: str) -> List[Dict[str, Any]]:
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

def _approx(a: Optional[float], b: Optional[float], tol: float = 1.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
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

# =========================
#  pdfplumber — tables()
# =========================
def _parse_lines_with_pdfplumber_tables(pdf_path: str) -> List[Dict[str, Any]]:
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = []
                # 2 stratégies
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
        # dédoublonnage simple
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

# ============================================
#  pdfplumber — reconstruction par positions
# ============================================
def _parse_lines_by_xpos(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Méthode robuste pour des PDFs où le texte linéarisé casse les lignes.
    - détecte les x des colonnes à partir des en-têtes,
    - “range” chaque mot dans la bonne colonne selon x,
    - regroupe par lignes (y) et recompose ref/label/qty/pu/amount.
    """
    if pdfplumber is None:
        return []

    rows: List[Dict[str, Any]] = []

    def norm(s: str) -> str:
        return _norm_header_cell(s)

    HEADERS_FLAT = [h for group in TABLE_HEADER_HINTS for h in group]

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1, y_tolerance=2, keep_spaces=True, use_text_flow=True)
                if not words:
                    continue

                # 1) trouver les en-têtes et leur x_center
                header_words = []
                for w in words:
                    txt = norm(w.get("text", ""))
                    if any(h in txt for h in HEADERS_FLAT):
                        header_words.append(w)
                # fallback : parfois les headers sont sur plusieurs mots (“prix”, “unitaire”)
                # on prend une ligne la plus haute qui contient certains mots-clés
                if not header_words:
                    for w in words:
                        if norm(w.get("text","")) in ("ref","designation","desigation","libelle","qte","qty","pu","montant","total","amount"):
                            header_words.append(w)

                # si on ne trouve pas d’en-têtes, on ne peut pas déduire les colonnes
                if not header_words:
                    continue

                # 2) détecter la bande Y des en-têtes (la ligne la plus haute contenant le plus d’indices)
                header_y = min(w["top"] for w in header_words)
                # on prend tous les mots sur une bande +/- 10 px
                band = [w for w in header_words if abs(w["top"] - header_y) <= 10]

                # 3) classer par x et créer des limites de colonnes
                band_sorted = sorted(band, key=lambda w: w["x0"])
                # associer les header->role
                column_roles: List[Tuple[str, float]] = []
                for w in band_sorted:
                    label = norm(w["text"])
                    role = None
                    if any(k in label for k in TABLE_HEADER_HINTS[0]): role = "ref"
                    elif any(k in label for k in TABLE_HEADER_HINTS[1]): role = "label"
                    elif any(k in label for k in TABLE_HEADER_HINTS[2]): role = "qty"
                    elif any(k in label for k in TABLE_HEADER_HINTS[3]): role = "unit_price"
                    elif any(k in label for k in TABLE_HEADER_HINTS[4]): role = "amount"
                    if role:
                        xcenter = (w["x0"] + w["x1"]) / 2.0
                        column_roles.append((role, xcenter))

                if not column_roles:
                    continue

                # bornes de colonnes : milieu entre deux centres successifs
                column_roles = sorted(column_roles, key=lambda t: t[1])
                boundaries = []
                for i, (_, x) in enumerate(column_roles):
                    if i == 0:
                        left = x - 9999
                    else:
                        left = (column_roles[i-1][1] + x) / 2.0
                    if i == len(column_roles) - 1:
                        right = x + 9999
                    else:
                        right = (x + column_roles[i+1][1]) / 2.0
                    role = column_roles[i][0]
                    boundaries.append((role, left, right))

                # 4) regrouper les mots en “lignes visuelles”
                # on ignore la zone haute des en-têtes (>= header_y - 5)
                content_words = [w for w in words if w["top"] > header_y + 5]
                # cluster par y avec tolérance
                lines: List[List[dict]] = []
                y_tol = 4.0
                for w in content_words:
                    y = (w["top"] + w["bottom"]) / 2.0
                    placed = False
                    for line in lines:
                        y_line = sum(((ww["top"]+ww["bottom"])/2.0) for ww in line) / len(line)
                        if abs(y - y_line) <= y_tol:
                            line.append(w)
                            placed = True
                            break
                    if not placed:
                        lines.append([w])

                # 5) pour chaque ligne, ranger les mots dans la colonne la plus probable
                for line in lines:
                    cols: Dict[str, List[str]] = {"ref":[], "label":[], "qty":[], "unit_price":[], "amount":[]}
                    for w in sorted(line, key=lambda ww: ww["x0"]):
                        xmid = (w["x0"] + w["x1"]) / 2.0
                        txt = w.get("text","").strip()
                        # ignorer lignes totaux (Total HT/TVA/TTC)
                        if re.search(r'^\s*(total|tva)\b', _norm_header_cell(txt)):
                            cols = {}
                            break
                        # trouver la colonne
                        target = None
                        for role, left, right in boundaries:
                            if left <= xmid <= right:
                                target = role
                                break
                        if not target:
                            continue
                        cols[target].append(txt)
                    if not cols:
                        continue

                    # composer les valeurs
                    label = " ".join(cols.get("label") or []).strip() or None
                    ref   = " ".join(cols.get("ref")   or []).strip() or None
                    qty_s = " ".join(cols.get("qty")   or []).strip() or ""
                    unit_s= " ".join(cols.get("unit_price") or []).strip() or ""
                    amt_s = " ".join(cols.get("amount")     or []).strip() or ""

                    # nettoyer montant : garder le dernier token monétaire si concat
                    def last_amount(s: str) -> Optional[float]:
                        if not s:
                            return None
                        cands = re.findall(r'[0-9][0-9\.\,\s]*', s)
                        if not cands:
                            return _norm_amount(s)
                        return _norm_amount(cands[-1])

                    # qty
                    qty = None
                    if qty_s:
                        try:
                            qty = int(re.sub(r"[^\d]", "", qty_s)) if re.search(r"\d", qty_s) else None
                        except Exception:
                            qty = None

                    pu_f  = last_amount(unit_s)
                    amt_f = last_amount(amt_s)

                    # ignorer lignes vides/bruit
                    if not any([label, ref, qty, pu_f, amt_f]):
                        continue

                    if not label and ref:
                        label = ref

                    rows.append({
                        "ref":        ref,
                        "label":      label,
                        "qty":        qty,
                        "unit_price": pu_f,
                        "amount":     amt_f
                    })

        # dédoublonner & filtrer le bruit
        out, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            # éliminer les lignes “total …”
            if r.get("label") and re.search(r'^\s*(total|tva)\b', _norm_header_cell(r["label"] or "")):
                continue
            out.append(r)
        return out
    except Exception:
        return []

# =========================
#    API PRINCIPALE
# =========================
def _extract_text(path: Path) -> str:
    try:
        return _extract_text(path) or ""
    except Exception:
        return ""

def extract_document(path: str, ocr: str = "auto") -> Dict[str, Any]:
    p = Path(path)
    text = _extract_text(p)

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
    }

    # champs simples
    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    m_total = TOTAL_RE.search(text)
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
    if total_ttc is None:
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            # éviter de prendre des gros IBAN tronqués : on filtre > 0 et <= 1e7
            total_ttc = max([a for a in amounts if 0 < a < 1e7], default=None)

    # devise
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"

    # taux TVA
    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vr = m_vat.group(1)
        vat_rate = '5.5' if vr in ('5,5', '5.5') else vr

    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "total_ht":  None,
            "total_tva": None,
            "total_ttc": total_ttc,
            "currency":  currency or "EUR",
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # blocs vendeur/client
    m = SELLER_BLOCK.search(text)
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))

    m = CLIENT_BLOCK.search(text)
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

    # identifiants
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

    # --------- LIGNES D'ARTICLES ----------
    lines: List[Dict[str, Any]] = []

    # 1) méthode robuste par X (nouvelle)
    if not lines:
        try:
            lines = _parse_lines_by_xpos(str(p))
        except Exception:
            lines = []

    # 2) si rien, tenter pdfplumber tables()
    if not lines:
        try:
            lines = _parse_lines_with_pdfplumber_tables(str(p))
        except Exception:
            lines = []

    # 3) si rien, fallback regex (texte linéarisé)
    if not lines:
        lines = _parse_lines_regex(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None

        if fields.get("total_ttc") and sum_lines and _approx(sum_lines, fields["total_ttc"], tol=1.5):
            total_ht, total_tva, total_ttc2 = _infer_totals(fields["total_ttc"], None, None, vat_rate)
            fields["total_ht"]  = total_ht
            fields["total_tva"] = total_tva
            fields["total_ttc"] = total_ttc2 or fields["total_ttc"]
        else:
            total_ht = sum_lines if sum_lines else fields.get("total_ht")
            th, tv, tt = _infer_totals(fields.get("total_ttc"), total_ht, fields.get("total_tva"), vat_rate)
            if th is not None: fields["total_ht"]  = th
            if tv is not None: fields["total_tva"] = tv
            if tt is not None: fields["total_ttc"] = tt

    return result

# alias pour compatibilité
def extract_pdf(path: str, ocr: str = "auto") -> Dict[str, Any]:
    return extract_document(path, ocr=ocr)
