from __future__ import annotations
import warnings
from pathlib import Path
from typing import Dict, Tuple, Union

from pdfminer.high_level import extract_text as _pdfminer_extract_text

# Optional CV stack for better OCR
try:
    import cv2  # type: ignore
except Exception:
    cv2 = None  # type: ignore

try:
    import pytesseract
    from PIL import Image, ImageOps, UnidentifiedImageError
except Exception:
    pytesseract = None  # type: ignore
    Image = None        # type: ignore
    ImageOps = None     # type: ignore
    UnidentifiedImageError = Exception  # type: ignore

# Optional handwriting OCR (install paddleocr or rapidocr-onnxruntime to enable)
try:
    from paddleocr import PaddleOCR  # type: ignore
except Exception:
    PaddleOCR = None  # type: ignore


def pdf_text(path: Union[str, Path]) -> str:
    try:
        return _pdfminer_extract_text(str(path)) or ""
    except Exception:
        return ""


def _opencv_preprocess(p: Path) -> "Image.Image":
    """Deskew + denoise + adaptive threshold (if OpenCV available)."""
    if cv2 is None or Image is None:
        # Fallback simple PIL binarization
        img = Image.open(str(p))
        g = ImageOps.grayscale(img)
        return g.point(lambda x: 255 if x > 180 else 0, mode="1")

    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR) if False else cv2.imread(str(p))
    if img is None:
        raise UnidentifiedImageError("Cannot open image")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Denoise & normalize
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    # Deskew (estimate angle by Hough on edges)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLines(edges, 1, 3.14159/180, 120)
    angle = 0.0
    if lines is not None:
        import numpy as np
        # average angle around horizontal/vertical
        for rho_theta in lines[:20]:
            rho, theta = rho_theta[0]
            a = (theta - 3.14159/2) * 180 / 3.14159
            angle += a
        angle = angle / min(len(lines), 20)
    if abs(angle) > 0.5:
        (h, w) = gray.shape[:2]
        M = cv2.getRotationMatrix2D((w//2, h//2), angle, 1.0)
        gray = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    thr = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 35, 15)
    pil = Image.fromarray(thr)
    return pil


def _tesseract_ocr(img: "Image.Image", lang: str = "fra+eng") -> Tuple[str, Dict[str, str]]:
    if pytesseract is None:
        return "", {"error": "tesseract_not_found"}
    try:
        import shutil
        tpath = shutil.which("tesseract")
        if tpath:
            pytesseract.pytesseract.tesseract_cmd = tpath
    except Exception:
        pass

    try:
        # timeout hard stop to avoid wedges on bad scans
        txt = pytesseract.image_to_string(img, lang=lang, timeout=20)  # type: ignore[arg-type]
        return txt or "", {"ocr_engine": "tesseract", "ocr_lang": lang}
    except pytesseract.TesseractError as e:  # type: ignore[attr-defined]
        return "", {"error": "tesseract_error", "details": str(e)}
    except Exception as e:
        return "", {"error": "tesseract_exception", "details": f"{type(e).__name__}: {e}"}


def _handwriting_ocr(p: Path) -> Tuple[str, Dict[str, str]]:
    """Optional handwriting engine via PaddleOCR if available."""
    if PaddleOCR is None:
        return "", {"error": "handwriting_engine_unavailable"}
    try:
        # latin best model
        ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        res = ocr.ocr(str(p), cls=True)
        lines = []
        for page in res or []:
            for line in page or []:
                if not line or len(line) < 2:
                    continue
                text = (line[1] or ["", 0])[0]
                lines.append(text)
        return "\n".join(lines), {"ocr_engine": "paddleocr"}
    except Exception as e:
        return "", {"error": "handwriting_exception", "details": str(e)}


def ocr_image_to_text(path: Union[str, Path], lang: str = "fra+eng") -> Tuple[str, Dict[str, object]]:
    """
    Robust OCR: OpenCV preprocessing -> Tesseract. If result too weak and handwriting
    engine is present, try it as fallback. Always return info dict (no exceptions).
    """
    p = Path(path)
    meta: Dict[str, object] = {"ocr_used": True}

    if Image is None:
        return "", {"error": "pillow_missing"}

    # Preprocess
    try:
        if cv2 is not None:
            img = _opencv_preprocess(p)
        else:
            img = Image.open(str(p))
            g = ImageOps.grayscale(img)
            img = g.point(lambda x: 255 if x > 180 else 0, mode="1")
    except UnidentifiedImageError as e:
        return "", {"error": "bad_image", "details": str(e)}
    except Exception as e:
        return "", {"error": "preprocess_error", "details": f"{type(e).__name__}: {e}"}

    # First pass: Tesseract
    txt, info = _tesseract_ocr(img, lang=lang)
    meta.update(info)
    if txt and len(txt.strip()) >= 10:
        return txt, meta

    # Fallback: handwriting OCR if available
    htxt, hinter = _handwriting_ocr(Path(path))
    if htxt and len(htxt.strip()) >= 10:
        meta.update(hinter)
        return htxt, meta

    # Give best we got
    if not txt and not htxt:
        meta.update(hinter if 'error' in hinter else {})
    return (txt or htxt or ""), meta
