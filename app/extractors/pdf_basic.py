# app/extractors/pdf_basic.py
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# ---- Imports parsers ----
from dateutil import parser as dateparser

# pdfminer (texte natif rapide)
from pdfminer.high_level import extract_text as _extract_text

# pdfplumber (tables + détection de texte par page)
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# OCR (facultatif)
try:
    import pytesseract
except Exception:
    pytesseract = None

# Pillow pour pré-traitement image OCR
try:
    from PIL import Image, ImageOps, ImageFilter
except Exception:
    Image = None

# OpenCV (accélère & deskew si dispo)
try:
    import cv2
except Exception:
    cv2 = None


# ==============================
#           REGEX
# ==============================
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
    r'(?:Client|Acheteur|Buyer|Bill\s*to)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)

# Lignes fallback “texte brut” (référence — libellé + 3 nombres éparpillés)
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)$',
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


# ==============================
#         HELPERS généraux
# ==============================
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

def _approx(a: Optional[float], b: Optional[float], tol: float = 1.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0

    ht, tva, ttc = total_ht, total_tva, total_ttc

    # TTC connu → calcule HT/TVA si manquants
    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht is None:  ht = ht_calc
            if tva is None: tva = tva_calc
        except Exception:
            pass

    # HT connu → calcule TVA/TTC si manquants
    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception:
            pass

    # TTC & TVA connus → calc HT
    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc


# ==============================
#          OCR accéléré
# ==============================
def _deskew_opencv(img_cv):
    # Calcul d’angle via minAreaRect
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    gray = cv2.bitwise_not(gray)
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(thresh)
    if coords is None:
        return img_cv
    rect = cv2.minAreaRect(coords)
    angle = rect[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    (h, w) = img_cv.shape[:2]
    M = cv2.getRotationMatrix2D((w//2, h//2), angle, 1.0)
    return cv2.warpAffine(img_cv, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

def _preprocess_pil(img: "Image.Image") -> "Image.Image":
    # Gris → contraste → sharpen → binaire
    img = ImageOps.grayscale(img)
    # upscale léger
    w, h = img.size
    if max(w, h) < 1600:
        img = img.resize((int(w*1.5), int(h*1.5)))
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=180, threshold=4))
    # binarisation douce
    img = ImageOps.autocontrast(img)
    return img

def _ocr_page_pil(pil_img: "Image.Image") -> str:
    if pytesseract is None:
        return ""
    img = pil_img
    if cv2 is not None:
        # deskew via OpenCV si dispo
        try:
            img_cv = cv2.cvtColor(cv2.imread(os.devnull), cv2.COLOR_BGR2RGB)  # dummy to get dtype
        except Exception:
            img_cv = None
        try:
            arr = cv2.cvtColor(
                cv2.imread(os.devnull),  # dummy line to keep cv2 import in some envs
                cv2.COLOR_BGR2RGB
            )
        except Exception:
            pass
        # conversion PIL -> CV2
        try:
            import numpy as np
            img_cv = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
            img_cv = _deskew_opencv(img_cv)
            img = Image.fromarray(cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB))
        except Exception:
            pass

    try:
        img = _preprocess_pil(img)
    except Exception:
        pass

    try:
        return pytesseract.image_to_string(img, lang="fra+eng")
    except Exception:
        return ""

def _extract_text_with_ocr_auto(pdf_path: str) -> str:
    """
    Fusionne texte natif (pdfminer) + OCR page-par-page uniquement quand nécessaire.
    Accélère : on OCR seulement les pages "muettes".
    """
    # 1) Essai texte natif global (rapide)
    text_natif = ""
    try:
        text_natif = _extract_text(pdf_path) or ""
    except Exception:
        text_natif = ""

    # Si déjà beaucoup de texte → retourne
    if text_natif and len(text_natif.strip()) > 50:
        return text_natif

    # 2) Page-par-page avec pdfplumber (détecter pages muettes)
    if pdfplumber is None or Image is None:
        # Fallback : au moins retourne le natif
        return text_natif

    pages_text: List[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t.strip():
                    pages_text.append(t)
                    continue

                # Page muette → OCR
                pil = page.to_image(resolution=200).original  # PIL image
                ocr_txt = _ocr_page_pil(pil) if pil else ""
                pages_text.append(ocr_txt)
    except Exception:
        # Dernier fallback
        return text_natif

    merged = "\n\f\n".join(pages_text)
    # si ça n’a rien donné, au moins le natif
    return merged or text_natif


# ==============================
#   pdfplumber : tables fiabilisées
# ==============================
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

def _extract_tables_plumber(pdf_path: str) -> List[List[List[str]]]:
    """
    Essaie plusieurs stratégies d’extraction de tables et renvoie une liste de tables candidates.
    Chaque table = list[rows], row = list[cells]
    """
    if pdfplumber is None:
        return []
    tables_all: List[List[List[str]]] = []
    settings_list = [
        None,  # extract_table() défaut
        {"vertical_strategy":"lines", "horizontal_strategy":"lines"},
        {"vertical_strategy":"text",  "horizontal_strategy":"text"},
    ]
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for st in settings_list:
                    try:
                        t = page.extract_table(table_settings=st) if st else page.extract_table()
                    except TypeError:
                        # versions anciennes: arg s’appelle différemment
                        try:
                            t = page.extract_table(st) if st else page.extract_table()
                        except Exception:
                            t = None
                    except Exception:
                        t = None
                    if not t:
                        continue
                    # Nettoyage
                    t = [[(c or "").strip() for c in (row or [])] for row in (t or []) if any((row or []))]
                    if t and len(t) >= 2:
                        tables_all.append(t)
    except Exception:
        pass
    return tables_all

def _parse_lines_from_tables(pdf_path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for tbl in _extract_tables_plumber(pdf_path):
        header = tbl[0]
        idx = _map_header_indices(header)
        if not idx:
            continue
        for line in tbl[1:]:
            def get(i): return line[i] if (i is not None and i < len(line)) else ""
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
    # Dédoublonnage
    uniq, seen = [], set()
    for r in rows:
        key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
        if key in seen: 
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


# ==============================
#     Fallback “texte brut”
#   (recolle qté / PU / montant
#    dispersés sur lignes suivantes)
# ==============================
NUM_TOKEN = re.compile(r'^(?:\d{1,3}(?:[ \.,]\d{3})*(?:[\,\.]\d{2})?)(?:\s*€)?$')

def _collect_next_numbers(lines: List[str], start: int, want: int = 3) -> Tuple[List[str], int]:
    """
    À partir d’un index, ramasse jusqu’à `want` tokens numériques (qty, pu, amt)
    en sautant les lignes vides / non numériques. Renvoie (tokens, next_index).
    """
    out = []
    i = start
    while i < len(lines) and len(out) < want:
        t = lines[i].strip()
        if NUM_TOKEN.match(t):
            out.append(t)
        i += 1
    return out, i

def _parse_lines_from_text(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # On part des libellés “REF — Désignation”
    # puis on cherche 1..3 nombres après (qty, pu, amt) sur les lignes suivantes.
    raw_lines = [l for l in text.splitlines() if l.strip()]
    for i, line in enumerate(raw_lines):
        m = LINE_RX.match(line.strip())
        if not m:
            continue
        ref   = m.group('ref').strip()
        label = m.group('label').strip()

        nums, _ = _collect_next_numbers(raw_lines, i+1, want=3)
        qty = None; pu_f = None; amt_f = None
        if nums:
            # Heuristique : si 3 nombres → qty, pu, amt
            # si 2 nombres → pu, amt
            # si 1 nombre → amt
            if len(nums) == 3:
                try:
                    qty = int(re.sub(r"[^\d]", "", nums[0]))
                except Exception:
                    qty = None
                pu_f  = _norm_amount(nums[1])
                amt_f = _norm_amount(nums[2])
            elif len(nums) == 2:
                pu_f  = _norm_amount(nums[0])
                amt_f = _norm_amount(nums[1])
            elif len(nums) == 1:
                amt_f = _norm_amount(nums[0])

        rows.append({
            "ref":        ref or None,
            "label":      label,
            "qty":        qty,
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


# ==============================
#       Extraction principale
# ==============================
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)

    # Texte : natif + OCR auto si nécessaire (amélioration perf)
    text = ""
    try:
        # Permettre de forcer OCR via env (utile debug) : BX_OCR=always/never/auto
        ocr_mode = os.getenv("BX_OCR", "auto").lower()
        if ocr_mode == "never":
            text = _extract_text(str(p)) or ""
        elif ocr_mode == "always":
            text = _extract_text_with_ocr_auto(str(p))  # fait aussi le natif si dispo, sinon OCR
        else:  # auto
            text = _extract_text_with_ocr_auto(str(p))
    except Exception:
        text = _extract_text(str(p)) or ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": (text.count("\f") + 1) if text else 0,
        "filename": p.name,
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
            "total_ht":  None,
            "total_tva": None,
            "total_ttc": total_ttc,
            "currency":  currency or "EUR",
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # Vendeur / Client
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

    # ============ LIGNES ============
    # 1) Tables pdfplumber fiabilisées
    lines: List[Dict[str, Any]] = []
    try:
        lines = _parse_lines_from_tables(str(p))
    except Exception:
        lines = []

    # 2) Fallback texte brut “réf — libellé” + recollement qty/PU/montant
    if not lines:
        try:
            lines = _parse_lines_from_text(text)
        except Exception:
            lines = []

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
