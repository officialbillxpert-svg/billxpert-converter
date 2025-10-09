
from __future__ import annotations
from typing import Tuple, Dict, Any, Optional, List
from pathlib import Path

# We keep imports optional to avoid hard failures at runtime
try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore

def pdf_text(path: Path) -> Tuple[str, Dict[str, Any]]:
    """
    Try to extract text from a PDF using PyPDF2 if available.
    Returns (text, info)
    """
    info: Dict[str, Any] = {"engine": "none"}
    text = ""
    try:
        import PyPDF2  # type: ignore
        info["engine"] = "PyPDF2"
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            pages = []
            for p in reader.pages:
                try:
                    pages.append(p.extract_text() or "")
                except Exception:
                    pages.append("")
            text = "\\n\\f\\n".join(pages).strip()
    except Exception as e:
        info["error"] = f"pdf_read_error:{e}"
    return text, info

def ocr_image_to_text(path: Path, lang: str = "fra") -> Tuple[str, Dict[str, Any]]:
    """
    OCR an image file if pytesseract is available.
    """
    info: Dict[str, Any] = {"engine": "pytesseract", "lang": lang}
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        img = Image.open(path)
        txt = pytesseract.image_to_string(img, lang=lang)
        return txt, info
    except Exception as e:
        info["error"] = f"ocr_error:{e}"
        return "", info

def pdf_ocr_text(path: Path, dpi: int = 200, lang: str = "fra") -> Tuple[str, Dict[str, Any]]:
    """
    Very defensive PDF > image OCR pipeline using pdf2image if present, else empty.
    """
    info: Dict[str, Any] = {"engine": "pdf_ocr", "dpi": dpi, "lang": lang}
    try:
        from pdf2image import convert_from_path  # type: ignore
        import pytesseract  # type: ignore
        pages = convert_from_path(str(path), dpi=dpi)
        texts: List[str] = []
        for img in pages:
            texts.append(pytesseract.image_to_string(img, lang=lang) or "")
        return "\\n\\f\\n".join(texts).strip(), info
    except Exception as e:
        info["error"] = f"pdf_ocr_unavailable:{e}"
        return "", info
