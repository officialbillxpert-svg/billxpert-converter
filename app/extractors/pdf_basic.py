# app/extractors/pdf_basic.py
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# --- Imports texte PDF ---
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except Exception:
    pdfminer_extract_text = None  # type: ignore

# --- OCR / Image ---
try:
    import pytesseract
except Exception:
    pytesseract = None  # type: ignore

try:
    from PIL import Image, ImageOps, ImageFilter
except Exception:
    Image = None  # type: ignore

try:
    import cv2  # opencv-python-headless
except Exception:
    cv2 = None  # type: ignore

# --- Optionnel : pdfplumber pour tables structurées ---
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# ---------- Regex & Hints ----------
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')
TOTAL_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Montant\s*TTC|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# Blocs parties
SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Emetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Emetteur|Vendeur|Seller)',
    re.I | re.S
)

# Lignes article (fallback texte)
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Taux TVA
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

# ---------- Helpers ----------
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
        qty = None
        try: qty = int(m.group('qty'))
        except Exception: pass
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
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = []
                t = page.extract_table()
                if t: tables.append(t)
                try:
                    t2 = page.extract_table({"vertical_strategy":"lines", "horizontal_strategy":"lines"})
                    if t2: tables.append(t2)
                except Exception:
                    pass

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

# ---------- OCR image ----------
def _pil_from_path(img_path: str):
    if Image is None:
        return None
    try:
        im = Image.open(img_path)
        im = ImageOps.exif_transpose(im)  # corrige l'orientation
        if im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        return im
    except Exception:
        return None

def _preprocess_pil(im):
    """Petit boost lisibilité avant OCR."""
    if im is None:
        return None
    try:
        if cv2 is None:
            # PIL only
            g = im.convert("L")
            g = ImageOps.autocontrast(g)
            g = g.filter(ImageFilter.SHARPEN)
            return g
        # OpenCV pipeline
        import numpy as np
        arr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        gray = cv2.bilateralFilter(gray, 9, 75, 75)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return Image.fromarray(bw)
    except Exception:
        return im

def _ocr_image_to_text(img_path: str, lang: str = "fra+eng") -> str:
    if pytesseract is None or Image is None:
        return ""
    im = _pil_from_path(img_path)
    if im is None:
        return ""
    pim = _preprocess_pil(im)
    try:
        txt = pytesseract.image_to_string(
            pim,
            lang=lang,
            config="--oem 1 --psm 6"
        )
        return txt or ""
    except Exception:
        return ""

# ---------- Extraction principale ----------
def _basic_currency(text: str) -> Optional[str]:
    if re.search(r"\bEUR\b|€", text, re.I): return "EUR"
    if re.search(r"\bGBP\b|£", text, re.I): return "GBP"
    if re.search(r"\bCHF\b", text, re.I):   return "CHF"
    if re.search(r"\bUSD\b|\$", text, re.I):return "USD"
    return None

def extract_document(path: str, ocr: str = "auto") -> Dict[str, Any]:
    """
    Extraction unifiée :
      - PDF texte : pdfminer (puis parsing)
      - Image (PNG/JPG) : OCR (pytesseract)
    ocr: "auto" | "force" | "off"
    """
    p = Path(path)
    ext = p.suffix.lower()
    is_image = ext in {".png", ".jpg", ".jpeg"}

    text = ""
    used_ocr = False
    ocr_pages = 0
    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": 0,
        "filename": p.name,
    }

    # --- Images : OCR direct ---
    if is_image:
        if ocr != "off":
            used_ocr = True
            text = _ocr_image_to_text(str(p))
            ocr_pages = 1
        meta.update({"from_images": True, "ocr_used": used_ocr, "ocr_pages": ocr_pages})
    else:
        # --- PDF ---
        meta["pages"] = 0
        if pdfminer_extract_text is not None:
            try:
                text = pdfminer_extract_text(p) or ""
                # "\f" sépare les pages dans pdfminer
                meta["pages"] = (text.count("\f") + 1) if text else 0
            except Exception:
                text = ""

        # si on veut forcer l'OCR pour PDF scanné — (implémentation OCR PDF complète possible plus tard)
        # ici, on se contente du texte pdfminer ; pour OCR PDF scanné, on pourra ajouter pypdfium2 pour rasteriser.
        meta["ocr_used"] = False
        meta["ocr_pages"] = 0

    # === Parsing commun du texte ===
    fields: Dict[str, Any] = {
        "invoice_number": None,
        "invoice_date":   None,
        "total_ht":  None,
        "total_tva": None,
        "total_ttc": None,
        "currency":  _basic_currency(text) or "EUR",
    }

    # Numero
    m_num = NUM_RE.search(text)
    if m_num:
        fields["invoice_number"] = m_num.group(1).strip()

    # Date
    from dateutil import parser as dateparser
    m_date = DATE_RE.search(text)
    if m_date:
        try:
            fields["invoice_date"] = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            pass

    # Totaux
    m_total = TOTAL_RE.search(text)
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
    if total_ttc is None:
        amounts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amounts = [a for a in amounts if a is not None]
        if amounts:
            total_ttc = max(amounts)
    fields["total_ttc"] = total_ttc

    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vr = m_vat.group(1)
        vat_rate = '5.5' if vr in ('5,5', '5.5') else vr

    # Parties
    m = SELLER_BLOCK.search(text)
    if m:
        seller_blk = _clean_block(m.group('blk'))
        if seller_blk: fields["seller"] = seller_blk

    m = CLIENT_BLOCK.search(text)
    if m:
        buyer_blk = _clean_block(m.group('blk'))
        if buyer_blk: fields["buyer"] = buyer_blk

    m = TVA_RE.search(text)
    if m: fields["seller_tva"] = m.group(0).replace(' ', '')

    m = SIRET_RE.search(text)
    if m:
        fields["seller_siret"] = m.group(0)
    else:
        m2 = SIREN_RE.search(text)
        if m2:
            fields["seller_siret"] = m2.group(0)

    m = IBAN_RE.search(text)
    if m:
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # Lignes
    lines: List[Dict[str, Any]] = []
    if not is_image and pdfplumber is not None:
        try:
            lines = _parse_lines_with_pdfplumber(str(p))
        except Exception:
            lines = []
    if not lines:
        lines = _parse_lines(text)

    if lines:
        fields["lines_count"] = len(lines)

    # Totaux HT/TVA à partir des lignes et du taux
    sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None
    if total_ttc and sum_lines and _approx(sum_lines, total_ttc, tol=1.5):
        th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
        fields["total_ht"], fields["total_tva"], fields["total_ttc"] = th, tv, tt or total_ttc
    else:
        th = sum_lines if sum_lines else fields.get("total_ht")
        th2, tv2, tt2 = _infer_totals(total_ttc, th, fields.get("total_tva"), vat_rate)
        if th2 is not None: fields["total_ht"]  = th2
        if tv2 is not None: fields["total_tva"] = tv2
        if tt2 is not None: fields["total_ttc"] = tt2

    # Devise par défaut si rien trouvé
    if not fields.get("currency"):
        fields["currency"] = "EUR"

    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": fields,
        "text": (text or "")[:20000],
        "text_preview": (text or "")[:2000],
    }
    if lines:
        result["lines"] = lines

    # Si on est en mode image et que l’OCR est impossible
    if is_image and (pytesseract is None or not text.strip()):
        result["success"] = False
        result["error"] = "tesseract_not_working"
        result["details"] = "OCR image exécuté mais texte vide (vérifier qualité / prétraitement)."

    return result

# --- Compat ancien nom ---
def extract_pdf(path: str, ocr: str = "auto") -> Dict[str, Any]:
    return extract_document(path, ocr=ocr)
