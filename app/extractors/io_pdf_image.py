from __future__ import annotations
from typing import Tuple, Dict, Any, Optional, List
from pathlib import Path
from PIL import Image, ImageOps, ImageFilter
import io
import pytesseract

DEFAULT_TIMEOUT = 25

# ---------- Chargement & prétraitement images ----------

def _load_image(path: Path) -> Image.Image:
    with open(path, "rb") as f:
        img = Image.open(io.BytesIO(f.read()))
        img.load()
    return img

def _pil_basic_preprocess(img: Image.Image) -> Image.Image:
    g = img.convert("L")
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.MedianFilter(size=3))
    g = ImageOps.equalize(g)
    w, h = g.size
    max_dim = 2400
    scale = min(max_dim / max(w, h), 1.5)
    if scale != 1.0:
        g = g.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return g

def _cv2_binarize_if_available(img: Image.Image) -> Image.Image:
    try:
        import numpy as np
        import cv2
        arr = np.array(img.convert("L"))
        arr = cv2.equalizeHist(arr)
        thr = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 35, 11)
        return Image.fromarray(thr)
    except Exception:
        return img

def _detect_rotation_osd(img: Image.Image) -> int:
    try:
        w, h = img.size
        sf = 1600 / max(w, h) if max(w, h) > 1600 else 1.0
        probe = img if sf == 1.0 else img.resize((int(w * sf), int(h * sf)), Image.LANCZOS)
        osd = pytesseract.image_to_osd(probe, lang="eng")
        for line in osd.splitlines():
            if line.lower().startswith("rotate:"):
                deg = int("".join(ch for ch in line if ch.isdigit()))
                return (360 - deg) % 360
    except Exception:
        pass
    return 0

def _apply_rotation(img: Image.Image, deg: int) -> Image.Image:
    if deg in (0, 360, None):
        return img
    return img.rotate(deg, expand=True)

def _preprocess(img: Image.Image) -> Image.Image:
    rot = _detect_rotation_osd(img)
    if rot:
        img = _apply_rotation(img, rot)
    g = _pil_basic_preprocess(img)
    g = _cv2_binarize_if_available(g)
    return g

def _tesseract_try(img: Image.Image, lang: str, cfg: str, timeout: int) -> Tuple[str, Dict[str, Any]]:
    txt = pytesseract.image_to_string(img, lang=lang, config=cfg, timeout=timeout)
    txt = txt.replace("\x00", "").strip()
    return txt, {"ocr_lang": lang, "tesseract_config": cfg, "timeout_s": timeout}

def ocr_image_to_text(
    path_or_image: Path | Image.Image,
    lang: str = "fra+eng",
    timeout: Optional[int] = None
) -> Tuple[str, Dict[str, Any]]:
    timeout = timeout or DEFAULT_TIMEOUT
    try:
        img = path_or_image if isinstance(path_or_image, Image.Image) else _load_image(Path(path_or_image))
    except Exception as e:
        return "", {"error": "load_image_failed", "details": str(e)}

    warnings: List[str] = []
    try:
        pim = _preprocess(img)
    except Exception as e:
        pim = img
        warnings.append(f"preprocess_failed:{e}")

    profiles = [
        "--oem 3 --psm 6",
        "--oem 1 --psm 3",
        "--oem 3 --psm 4",
        "--oem 3 --psm 11",
        "--oem 0 --psm 6",
    ]

    def _run_profiles(img_: Image.Image, lang_: str) -> Tuple[str, Dict[str, Any]]:
        for cfg in profiles:
            try:
                txt, info = _tesseract_try(img_, lang_, cfg, timeout)
                info["warnings"] = warnings
                if txt:
                    info["tried_lang"] = lang_
                    return txt, info
            except Exception as e:
                warnings.append(str(e))
        return "", {"error": "ocr_failed", "warnings": warnings}

    txt, info = _run_profiles(pim, lang)
    if not txt:
        txt2, info2 = _run_profiles(pim, "eng")
        if txt2:
            return txt2, info2
    return txt, info

# ---------- OCR PDF ----------

def _pdf_to_images_pypdfium2(path: Path, dpi: int) -> List[Image.Image]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    images: List[Image.Image] = []
    for i in range(len(pdf)):
        page = pdf[i]
        pil = page.render(scale=dpi / 72.0).to_pil()
        rot = _detect_rotation_osd(pil)
        if rot:
            pil = _apply_rotation(pil, rot)
        images.append(pil)
    pdf.close()
    return images

def pdf_ocr_text(
    path: Path,
    lang: str = "fra+eng",
    max_pages: int = 5,
    dpi: int = 280,
    timeout_per_page: int = 30
) -> Tuple[str, Dict[str, Any]]:
    info: Dict[str, Any] = {"ocr_lang": lang, "ocr_pages": 0, "passes": []}
    try:
        imgs = _pdf_to_images_pypdfium2(path, dpi)
    except Exception as e:
        return "", {"error": "pdf_to_image_unavailable", "details": str(e)}

    texts: List[str] = []
    warnings: List[str] = []
    for i, im in enumerate(imgs[:max_pages]):
        try:
            # FIX: on ne double plus le preprocess
            t, run = ocr_image_to_text(im, lang=lang, timeout=timeout_per_page)
            if run.get("warnings"):
                warnings.extend(run["warnings"])
            texts.append(t or "")
        except Exception as e:
            warnings.append(f"page_{i+1}_error:{e}")
            texts.append("")

    full = "\n\f\n".join(texts).strip()
    info["ocr_pages"] = len(imgs)
    info["warnings"] = warnings
    if not full:
        info["error"] = "pdf_ocr_empty"
        return "", info
    return full, info
