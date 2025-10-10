# app/extractors/io_pdf_image.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, Tuple, Optional, List
import os

import numpy as np
from PIL import Image, ImageOps
import pytesseract
import pypdfium2 as pdfium

# PaddleOCR est optionnel : import paresseux
_PADDLE_OCR = None  # type: ignore

def _tess_lang() -> str:
    return os.getenv("OCR_LANG", "fra")

def _tess_config() -> str:
    # LSTM, bloc de texte, moins d'auto-corrections, blacklist de chars parasites
    return (
        "--oem 1 --psm 6 "
        "-c load_system_dawg=0 -c load_freq_dawg=0 "
        "-c tessedit_char_blacklist=\"|{}[]<>\\/@#~^*_`\""
    )

def _render_pdf_to_images(p: Path, dpi: int = 300, max_pages: Optional[int] = None) -> List[Image.Image]:
    doc = pdfium.PdfDocument(str(p))
    n = len(doc)
    limit = min(n, max_pages) if (isinstance(max_pages, int) and max_pages > 0) else n
    imgs: List[Image.Image] = []
    for i in range(limit):
        page = doc.get_page(i)
        pil = page.render(scale=dpi/72.0).to_pil()
        page.close()
        # Prétraitement léger : niveaux de gris + contraste/binarisation
        pil = ImageOps.grayscale(pil)
        imgs.append(pil)
    doc.close()
    return imgs

def pdf_text(p: Path) -> Tuple[str, Dict]:
    """
    Extraction texte rapide (si tu as un parseur PDF textuel, branche-le ici).
    On renvoie vide par défaut pour pousser l'heuristique OCR sur PDF 'image'.
    """
    try:
        return "", {"engine": "PyPDF2"}
    except Exception as e:
        return "", {"engine": "none", "error": f"pdf_read_error:{type(e).__name__}:{e}"}

def pdf_ocr_text(p: Path) -> Tuple[str, Dict]:
    """
    OCR PDF via pypdfium2 -> PIL -> Tesseract (sans Poppler).
    """
    info: Dict = {"engine": "pytesseract", "lang": _tess_lang(), "dpi": 300}
    try:
        max_pages_env = os.getenv("MAX_PAGES")
        max_pages = int(max_pages_env) if (max_pages_env and max_pages_env.isdigit()) else None

        chunks = []
        for img in _render_pdf_to_images(p, dpi=300, max_pages=max_pages):
            txt = pytesseract.image_to_string(img, lang=_tess_lang(), config=_tess_config()) or ""
            txt = txt.replace("\u00a0", " ")
            if txt.strip():
                chunks.append(txt)
        full = "\n\n".join(chunks).strip()
        return full, info
    except Exception as e:
        info["error"] = f"ocr_error:{e}"
        return "", info

def ocr_image_to_text(p: Path) -> Tuple[str, Dict]:
    info: Dict = {"engine": "pytesseract", "lang": _tess_lang()}
    try:
        img = Image.open(str(p))
        img = ImageOps.grayscale(img)
        txt = pytesseract.image_to_string(img, lang=_tess_lang(), config=_tess_config()) or ""
        txt = txt.replace("\u00a0", " ")
        return txt, info
    except Exception as e:
        info["error"] = f"ocr_error:{e}"
        return "", info

# --------- PaddleOCR ---------

def _get_paddle(lang: str = "fr"):
    global _PADDLE_OCR
    if _PADDLE_OCR is None:
        from paddleocr import PaddleOCR  # import tardif
        # CPU par défaut; use_angle_cls=True pour corriger rotations
        _PADDLE_OCR = PaddleOCR(
            lang=lang, use_angle_cls=True, det=True, rec=True, show_log=False
        )
    return _PADDLE_OCR

def pdf_paddle_text(p: Path) -> Tuple[str, Dict]:
    """
    OCR PDF via PaddleOCR (det + rec). Concatenate lines by reading order.
    """
    info: Dict = {"engine": "paddleocr", "lang": "fr", "dpi": 300}
    try:
        max_pages_env = os.getenv("MAX_PAGES")
        max_pages = int(max_pages_env) if (max_pages_env and max_pages_env.isdigit()) else None

        ocr = _get_paddle(lang="fr")
        lines: List[str] = []
        for img in _render_pdf_to_images(p, dpi=300, max_pages=max_pages):
            arr = np.array(img)  # grayscale ok
            result = ocr.ocr(arr, cls=True)
            # result: list[pages] -> list[list[ [bbox, (text, score)], ...]]
            if result and result[0]:
                for det in result[0]:
                    text, score = det[1]
                    if text and score >= 0.5:
                        lines.append(text)
        full = "\n".join(lines).strip()
        return full, info
    except Exception as e:
        info["error"] = f"paddle_error:{e}"
        return "", info

def image_paddle_text(p: Path) -> Tuple[str, Dict]:
    info: Dict = {"engine": "paddleocr", "lang": "fr"}
    try:
        from paddleocr import PaddleOCR
        ocr = _get_paddle(lang="fr")
        img = Image.open(str(p)).convert("L")
        arr = np.array(img)
        lines: List[str] = []
        result = ocr.ocr(arr, cls=True)
        if result and result[0]:
            for det in result[0]:
                text, score = det[1]
                if text and score >= 0.5:
                    lines.append(text)
        return "\n".join(lines).strip(), info
    except Exception as e:
        info["error"] = f"paddle_error:{e}"
        return "", info