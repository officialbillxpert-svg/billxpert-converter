# app/extractors/pdf_basic.py
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from dateutil import parser as dateparser

# --- PDF texte
try:
    from pdfminer.high_level import extract_text as _extract_text
except Exception:
    _extract_text = None

# --- Optionnel : pdfplumber pour tableaux
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# --- OCR image
try:
    from PIL import Image, ImageFilter, ImageOps
except Exception:
    Image = None

try:
    import pytesseract
except Exception:
    pytesseract = None


# =========================
# Regex & constantes
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


# =========================
# Helpers communs
# =========================
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

def _parse_lines(text: str) -> List[Dict[str, Any]]:
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


# =========================
# pdfplumber helpers (tables)
# =========================
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


# =========================
# OCR image helpers
# =========================
def _have_ocr() -> bool:
    return pytesseract is not None and Image is not None

def _ocr_langs() -> str:
    return os.getenv("OCR_LANGS", "fra+eng")

def _preprocess_image(img: "Image.Image") -> "Image.Image":
    # Grayscale
    g = ImageOps.grayscale(img)
    # Upscale 3x (améliore nettement Tesseract sur photos d’écran)
    w, h = g.size
    scale = 3  # 300%
    g = g.resize((w*scale, h*scale), Image.Resampling.LANCZOS)
    # Légère réduction de bruit / lissage
    g = g.filter(ImageFilter.MedianFilter(size=3))
    # Binarisation Otsu “approx” (PIL ne fait pas Otsu natif, on fait simple)
    # On utilise autocontrast qui marche bien sur des factures propres
    g = ImageOps.autocontrast(g, cutoff=2)
    # Sharpen léger
    g = g.filter(ImageFilter.UnsharpMask(radius=1.2, percent=150, threshold=3))
    return g

def _ocr_image_to_text(img: "Image.Image") -> str:
    if not _have_ocr():
        return ""
    lang = _ocr_langs()
    # premier essai psm6
    cfg = "--oem 3 --psm 6"
    txt = pytesseract.image_to_string(img, lang=lang, config=cfg) or ""
    txt = txt.strip()
    if txt:
        return txt
    # deuxième essai : un peu plus zoomé + psm4
    w, h = img.size
    zoom = img.resize((int(w*1.33), int(h*1.33)), Image.Resampling.LANCZOS)
    cfg2 = "--oem 3 --psm 4"
    txt2 = pytesseract.image_to_string(zoom, lang=lang, config=cfg2) or ""
    return (txt2 or "").strip()

def _extract_text_from_image(path: Path) -> str:
    if not _have_ocr():
        return ""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            pim = _preprocess_image(im)
            return _ocr_image_to_text(pim)
    except Exception:
        return ""


# =========================
# Extraction principale
# =========================
def extract_document(path: str | Path, mime: Optional[str] = None) -> Dict[str, Any]:
    """
    Route unique : accepte PDF ou image (PNG/JPG).
    - PDF -> pdfminer/pdfplumber + heuristique
    - Image -> OCR (pytesseract) + mêmes regex
    """
    p = Path(path)
    ext = p.suffix.lower()
    is_image = ext in {".png", ".jpg", ".jpeg"}
    from_images = False
    ocr_used = False
    ocr_pages = 0

    # --- obtenir le texte brut
    text = ""
    if is_image:
        from_images = True
        if _have_ocr():
            ocr_used = True
            text = _extract_text_from_image(p)
            ocr_pages = 1
        else:
            text = ""
    else:
        # PDF
        if _extract_text is not None:
            text = _extract_text(p) or ""
        else:
            text = ""

        # Si texte PDF vide et OCR disponible, on pourrait rasterizer et OCRiser page par page.
        # (on garde simple ici: uniquement image → OCR ; amélioration possible plus tard)
        # -> on ne modifie pas ocr_used ici.

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if (text and not is_image) else (0 if not is_image else 0),
        "filename": p.name,
        "from_images": from_images,
        "ocr_used": ocr_used,
        "ocr_pages": ocr_pages
    }

    # Champs simples
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
            total_ttc = max(amounts)

    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"

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
        "text": text[:20000] if text else "",
        "text_preview": text[:2000] if text else "",
    }
    fields = result["fields"]

    # Blocs parties
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

    # Lignes d'articles : sur PDF on tente pdfplumber + fallback regex.
    lines: List[Dict[str, Any]] = []
    if not is_image:
        try:
            lines = _parse_lines_with_pdfplumber(str(p))
        except Exception:
            lines = []
        if not lines:
            lines = _parse_lines(text)
    else:
        # Image OCR -> uniquement regex (on n’a pas de structure tableau)
        lines = _parse_lines(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None

        if total_ttc and sum_lines and _approx(sum_lines, total_ttc, tol=1.5):
            total_ht, total_tva, total_ttc2 = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = total_ht
            fields["total_tva"] = total_tva
            fields["total_ttc"] = total_ttc2 or total_ttc
        else:
            total_ht = sum_lines if sum_lines else fields.get("total_ht")
            th, tv, tt = _infer_totals(total_ttc, total_ht, fields.get("total_tva"), vat_rate)
            if th is not None: fields["total_ht"]  = th
            if tv is not None: fields["total_tva"] = tv
            if tt is not None: fields["total_ttc"] = tt

    return result
