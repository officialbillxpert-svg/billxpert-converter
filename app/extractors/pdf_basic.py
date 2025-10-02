# app/extractors/pdf_basic.py
from __future__ import annotations
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# --- PDF texte (pdfminer) ---
from pdfminer.high_level import extract_text as _pdfminer_extract_text

# --- pdfplumber (tables + words + positions) ---
try:
    import pdfplumber  # optional but strongly recommended
except Exception:
    pdfplumber = None  # type: ignore

# --- OCR images (PNG/JPG) ---
try:
    import pytesseract
    from PIL import Image, ImageOps
except Exception:
    pytesseract = None  # type: ignore
    Image = None  # type: ignore
    ImageOps = None  # type: ignore

# --------------------------------------------------------------------
# Regex & Hints
# --------------------------------------------------------------------
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

LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

# --------------------------------------------------------------------
# Helpers simples
# --------------------------------------------------------------------
def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None

def _clean_block(s: str) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
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

# --------------------------------------------------------------------
# pdfplumber – stratégie 1 : par positions X/Y (robuste)
# --------------------------------------------------------------------
def _parse_lines_by_xpos(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Reconstitue le tableau d’articles en lisant les mots positionnés sous les en-têtes.
    Très robuste quand le texte est éclaté (chaque cellule séparée).
    """
    if pdfplumber is None:
        return []

    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
                if not words:
                    continue

                # Chercher la ligne d’en-têtes : on repère la présence de mots-clés
                # On groupe les words par bande horizontale (ligne approximative)
                lines_by_y: Dict[int, List[dict]] = {}
                for w in words:
                    mid_y = int((w["top"] + w["bottom"]) / 2)
                    lines_by_y.setdefault(mid_y, []).append(w)

                header_line_y = None
                header_cells: Dict[str, dict] = {}

                def norm(wtxt: str) -> str:
                    return _norm_header_cell(wtxt)

                for ykey, ws in sorted(lines_by_y.items(), key=lambda kv: kv[0]):
                    txts = [norm(w["text"]) for w in sorted(ws, key=lambda w: w["x0"])]
                    txt = " ".join(txts)
                    # Un peu souple : on veut au moins 2-3 indices d’en-tête présents
                    score = 0
                    hitmap: Dict[str, dict] = {}
                    for w in ws:
                        t = norm(w["text"])
                        if any(c in t for c in TABLE_HEADER_HINTS[2]):  # qty
                            score += 1; hitmap["qty"] = w
                        if any(c in t for c in TABLE_HEADER_HINTS[3]):  # unit price
                            score += 1; hitmap["unit"] = w
                        if any(c in t for c in TABLE_HEADER_HINTS[4]):  # amount
                            score += 1; hitmap["amount"] = w
                        if any(c in t for c in TABLE_HEADER_HINTS[1]):  # label
                            score += 1; hitmap["label"] = w
                        if any(c in t for c in TABLE_HEADER_HINTS[0]):  # ref
                            score += 1; hitmap["ref"] = w
                    if score >= 3:
                        header_line_y = ykey
                        header_cells = hitmap
                        break

                if header_line_y is None:
                    # Impossible de trouver des headers → page suivante
                    continue

                # Définir les frontières X des colonnes à partir des mots d’en-têtes trouvés
                # On ordonne par x0 croissant
                cols = []
                for role in ["ref", "label", "qty", "unit", "amount"]:
                    if role in header_cells:
                        w = header_cells[role]
                        cols.append((role, (w["x0"] + w["x1"]) / 2))
                # Si on n'a pas tout, on comble en se basant sur la distribution x
                cols = sorted(cols, key=lambda t: t[1])

                if not cols:
                    continue

                # Construire des bornes [x_left, x_right) par rôle
                col_bounds: List[Tuple[str, float, float]] = []
                for i, (role, xmid) in enumerate(cols):
                    if i == 0:
                        left = 0.0
                        right = (cols[i+1][1] + xmid) / 2 if i+1 < len(cols) else xmid + 9999
                    elif i == len(cols) - 1:
                        left = (cols[i-1][1] + xmid) / 2
                        right = 999999.0
                    else:
                        left = (cols[i-1][1] + xmid) / 2
                        right = (cols[i+1][1] + xmid) / 2
                    col_bounds.append((role, left, right))

                # Toutes les lignes sous l’en-tête (y > header_line_y + petite marge)
                body_lines = {yk: ws for yk, ws in lines_by_y.items() if yk > header_line_y + 5}

                # Grouper par bandes (tolérance verticale)
                # On construit des groupes en fusionnant les y proches
                bands: List[Tuple[int, List[dict]]] = []
                for yk in sorted(body_lines.keys()):
                    ws = sorted(body_lines[yk], key=lambda w: w["x0"])
                    if not bands:
                        bands.append((yk, ws))
                    else:
                        last_y, last_ws = bands[-1]
                        if abs(yk - last_y) <= 6:  # tolérance verticale (ajustable)
                            last_ws.extend(ws)
                        else:
                            bands.append((yk, ws))

                # Affecter chaque word à une colonne selon x et concaténer
                for _, ws in bands:
                    cells: Dict[str, List[str]] = {role: [] for (role, _, _) in col_bounds}
                    for w in ws:
                        xmid = (w["x0"] + w["x1"]) / 2
                        for role, left, right in col_bounds:
                            if left <= xmid < right:
                                cells[role].append(w["text"])
                                break

                    ref   = " ".join(cells.get("ref", [])).strip() or None
                    label = " ".join(cells.get("label", [])).strip() or None
                    qtys  = " ".join(cells.get("qty", [])).strip()
                    pu    = " ".join(cells.get("unit", [])).strip()
                    amt   = " ".join(cells.get("amount", [])).strip()

                    # Nettoyages
                    def _to_int(s: str) -> Optional[int]:
                        s2 = re.sub(r"[^\d]", "", s or "")
                        if not s2:
                            return None
                        try:
                            return int(s2)
                        except Exception:
                            return None

                    qty_i  = _to_int(qtys)
                    pu_f   = _norm_amount(pu)
                    amt_f  = _norm_amount(amt)

                    # ligne valide si au moins label ou prix/amount
                    if not (label or pu_f is not None or amt_f is not None or qty_i is not None):
                        continue

                    # Label fallback
                    if (not label) and ref:
                        label = ref

                    rows.append({
                        "ref":        ref,
                        "label":      label or "",
                        "qty":        qty_i,
                        "unit_price": pu_f,
                        "amount":     amt_f
                    })

        # Dédoublonnage simple
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

# --------------------------------------------------------------------
# pdfplumber – stratégie 2 : tables explicites (si présentes)
# --------------------------------------------------------------------
def _parse_lines_extract_table(pdf_path: str) -> List[Dict[str, Any]]:
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
                        def get(i: Optional[int]) -> str:
                            return line[i] if (i is not None and i < len(line)) else ""
                        ref   = get(idx.get("ref"))
                        label = get(idx.get("label")) or ref
                        qty   = get(idx.get("qty"))
                        pu    = get(idx.get("unit"))
                        amt   = get(idx.get("amount"))

                        try:
                            qty = int(re.sub(r"[^\d]", "", qty)) if qty else None
                        except Exception:
                            qty = None

                        pu_f  = _norm_amount(pu)
                        amt_f = _norm_amount(amt)

                        if not (label or pu_f is not None or amt_f is not None):
                            continue

                        rows.append({
                            "ref":        (ref or "").strip() or None,
                            "label":      (label or "").strip(),
                            "qty":        qty,
                            "unit_price": pu_f,
                            "amount":     amt_f
                        })
        # dedup
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

# --------------------------------------------------------------------
# Extraction texte (PDF) / OCR (Images)
# --------------------------------------------------------------------
def _pdf_text(path: Union[str, Path]) -> str:
    try:
        return _pdfminer_extract_text(str(path)) or ""
    except Exception:
        return ""

def _ocr_image_to_text(path: Union[str, Path], lang: str = "fra+eng") -> Tuple[str, Dict[str, Any]]:
    if pytesseract is None or Image is None:
        return "", {"error": "tesseract_not_found", "details": "Binaire tesseract absent."}
    try:
        img = Image.open(str(path))
        # prétraitement simple : niveaux de gris + binarisation auto
        img = ImageOps.grayscale(img)
        # Optionnel : légère augmentation de contraste
        # img = ImageOps.autocontrast(img)
        txt = pytesseract.image_to_string(img, lang=lang) or ""
        return txt, {}
    except pytesseract.TesseractNotFoundError:
        return "", {"error": "tesseract_not_found", "details": "Binaire tesseract absent."}
    except Exception as e:
        return "", {"error": "ocr_failed", "details": f"{type(e).__name__}: {e}"}

# --------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------
def extract_document(path: str, ocr: str = "auto") -> Dict[str, Any]:
    """
    Extraction unifiée :
      - PDF texte → pdfminer + (lignes via xpos → table → regex).
      - Images (PNG/JPG) → OCR via tesseract (fra+eng).
    ocr: "auto" (par défaut), "force", "off" (pour PDF seulement).
    """
    p = Path(path)
    ext = p.suffix.lower()

    # Meta de base
    result: Dict[str, Any] = {
        "success": True,
        "meta": {
            "bytes": p.stat().st_size if p.exists() else None,
            "filename": p.name,
            "pages": 0,
            "ocr_used": False,
            "ocr_pages": 0,
            "from_images": ext in {".png", ".jpg", ".jpeg"},
            "line_strategy": "none",
        },
        "fields": {
            "invoice_number": None,
            "invoice_date":   None,
            "total_ht":  None,
            "total_tva": None,
            "total_ttc": None,
            "currency":  "EUR",
        },
        "text": "",
        "text_preview": "",
    }
    fields = result["fields"]

    # ------------------------------------------------------
    # 1) IMAGE → OCR
    # ------------------------------------------------------
    if ext in {".png", ".jpg", ".jpeg"}:
        txt, err = _ocr_image_to_text(p, lang="fra+eng")
        if err:
            result["success"] = False
            result.update(err)
            return result
        result["meta"]["ocr_used"] = True
        result["meta"]["ocr_pages"] = 1
        result["text"] = txt[:20000]
        result["text_preview"] = txt[:2000]

        # Champs rapides depuis texte OCR
        _fill_fields_from_text(result, txt)
        # Pas de lignes fiables via OCR sans layout → on laisse vide ici
        return result

    # ------------------------------------------------------
    # 2) PDF
    # ------------------------------------------------------
    # Texte brut
    text = _pdf_text(p) or ""
    result["text"] = text[:20000]
    result["text_preview"] = text[:2000]
    result["meta"]["pages"] = (text.count("\f") + 1) if text else 0

    # Champs depuis texte
    _fill_fields_from_text(result, text)

    # Lignes d’articles — stratégie 1 : X/Y
    lines = _parse_lines_by_xpos(str(p))
    if lines:
        result["lines"] = lines
        result["meta"]["line_strategy"] = "xpos"
    else:
        # Stratégie 2 : tables explicites
        lines = _parse_lines_extract_table(str(p))
        if lines:
            result["lines"] = lines
            result["meta"]["line_strategy"] = "table"
        else:
            # Stratégie 3 : regex brut
            lines = _parse_lines_regex(text)
            if lines:
                result["lines"] = lines
                result["meta"]["line_strategy"] = "regex"

    if lines:
        fields["lines_count"] = len(lines)
        # Totaux : tenter d’inférer HT/TVA si possible
        total_ttc = fields.get("total_ttc")
        # somme des montants (si présents)
        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2)
        vat_rate = _extract_vat_rate(text)
        if total_ttc and sum_lines and _approx(sum_lines, total_ttc, tol=1.5):
            th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = th
            fields["total_tva"] = tv
            fields["total_ttc"] = tt or total_ttc
        else:
            # considère la somme comme HT si pas TTC matching
            total_ht = sum_lines if sum_lines else fields.get("total_ht")
            th, tv, tt = _infer_totals(total_ttc, total_ht, fields.get("total_tva"), vat_rate)
            if th is not None: fields["total_ht"]  = th
            if tv is not None: fields["total_tva"] = tv
            if tt is not None: fields["total_ttc"] = tt

    return result

# --------------------------------------------------------------------
# Champs simples depuis le texte
# --------------------------------------------------------------------
def _extract_vat_rate(text: str) -> Optional[str]:
    m_vat = VAT_RATE_RE.search(text or "")
    if not m_vat:
        return None
    vr = m_vat.group(1)
    return '5.5' if vr in ('5,5', '5.5') else vr

def _fill_fields_from_text(result: Dict[str, Any], text: str) -> None:
    fields = result["fields"]

    m_num = NUM_RE.search(text or "")
    fields["invoice_number"] = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text or "")
    if m_date:
        try:
            from dateutil import parser as dateparser
            fields["invoice_date"] = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            fields["invoice_date"] = None

    m_total = TOTAL_RE.search(text or "")
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
    if total_ttc is None:
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)
    fields["total_ttc"] = total_ttc

    # currency
    if re.search(r"\bEUR\b|€", text, re.I): fields["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): fields["currency"] = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): fields["currency"] = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): fields["currency"] = "USD"

    # TVA rate (pour l’inférence)
    # (stocké implicitement, on ne l’expose pas directement)
    # seller / buyer blocks
    m = SELLER_BLOCK.search(text or "")
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))

    m = CLIENT_BLOCK.search(text or "")
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

    # ids FR
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
