# app/extractors/io_pdf_image.py
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
        thr = cv2.adaptiveThreshold(
            arr, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 35, 11
        )
        return Image.fromarray(thr)
    except Exception:
        return img

def _detect_rotation_osd(img: Image.Image) -> int:
    """
    Utilise Tesseract OSD pour estimer la rotation (0, 90, 180, 270).
    Retourne l'angle (degrés) à appliquer pour remettre le texte à l'endroit.
    """
    try:
        # on downsizze un peu pour éviter les timeouts
        w, h = img.size
        sf = 1600 / max(w, h) if max(w, h) > 1600 else 1.0
        probe = img if sf == 1.0 else img.resize((int(w * sf), int(h * sf)), Image.LANCZOS)
        osd = pytesseract.image_to_osd(probe, lang="eng")  # OSD aime bien "eng"
        # Ex: "Rotate: 90\nOrientation: ...\n"
        for line in osd.splitlines():
            line = line.strip().lower()
            if line.startswith("rotate:"):
                val = line.split(":")[1].strip()
                deg = int("".join(ch for ch in val if ch.isdigit()))
                # deg = 0, 90, 180, 270 (sens horaire)
                # pour corriger, on doit tourner dans l'autre sens
                return (360 - deg) % 360
    except Exception:
        pass
    return 0

def _apply_rotation(img: Image.Image, deg: int) -> Image.Image:
    if deg in (0, 360, None):
        return img
    return img.rotate(deg, expand=True)

def _preprocess(img: Image.Image) -> Image.Image:
    # 1) rotation auto sur l'image brute pour maximiser l'OSD
    rot = _detect_rotation_osd(img)
    if rot:
        img = _apply_rotation(img, rot)
    # 2) pipeline PIL
    g = _pil_basic_preprocess(img)
    # 3) binarisation (si opencv présent)
    g = _cv2_binarize_if_available(g)
    return g

def _tesseract_try(img: Image.Image, lang: str, cfg: str, timeout: int) -> Tuple[str, Dict[str, Any]]:
    txt = pytesseract.image_to_string(img, lang=lang, config=cfg, timeout=timeout)
    txt = txt.replace("\x00", "").strip()
    return txt, {"ocr_lang": lang, "tesseract_config": cfg, "timeout_s": timeout}

# ---------- OCR Image unique (PNG/JPG) ----------

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
        last_err_local: Optional[str] = None
        for cfg in profiles:
            try:
                txt, info = _tesseract_try(img_, lang_, cfg, timeout)
                info["warnings"] = warnings
                if txt:
                    info["tried_lang"] = lang_
                    return txt, info
                warnings.append(f"empty_text:{cfg}")
            except RuntimeError as e:
                last_err_local = f"RuntimeError:{e}"
                if "timeout" in str(e).lower():
                    try:
                        txt, info = _tesseract_try(img_, lang_, cfg, min(timeout + 5, timeout + 10))
                        info["warnings"] = warnings
                        if txt:
                            info["tried_lang"] = lang_
                            return txt, info
                    except RuntimeError as e2:
                        last_err_local = f"RuntimeError(retry):{e2}"
            except Exception as e:
                last_err_local = f"{type(e).__name__}:{e}"

        return "", {
            "error": "handwriting_engine_unavailable" if last_err_local and "timeout" in last_err_local.lower() else "ocr_failed",
            "details": last_err_local or "all_profiles_failed",
            "ocr_lang": lang_,
            "ocr_used": False,
            "warnings": warnings,
            "tried_lang": lang_,
        }

    txt, info = _run_profiles(pim, lang)
    if (not txt) and isinstance(info, dict) and ("details" in info):
        det = str(info.get("details") or "").lower()
        if "failed loading language" in det or "not available" in det or "tesseract couldn't load any languages" in det:
            warnings.append("lang_missing_fallback_to_eng")
            txt2, info2 = _run_profiles(pim, "eng")
            if txt2:
                return txt2, info2

    return txt, info

# ---------- Extraction texte natif PDF ----------

def pdf_text(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
        t = extract_text(str(path)) or ""
        if t and len(t.strip()) > 30:
            return t
    except Exception:
        pass
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                tt = page.extract_text() or ""
                if tt:
                    out.append(tt)
        return ("\n\n".join(out)).strip()
    except Exception:
        return ""

# ---------- OCR PDF (pypdfium2 + Tesseract) ----------

def _pdf_to_images_pypdfium2(path: Path, dpi: int) -> List[Image.Image]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        images: List[Image.Image] = []
        for i in range(len(pdf)):
            page = pdf[i]
            pil = page.render(scale=dpi / 72.0).to_pil()
            # rotation OSD avant préprocess (comme pour image)
            rot = _detect_rotation_osd(pil)
            if rot:
                pil = _apply_rotation(pil, rot)
            images.append(pil)
        return images
    finally:
        try:
            pdf.close()
        except Exception:
            pass

def pdf_ocr_text(
    path: Path,
    lang: str = "fra+eng",
    max_pages: int = 5,
    dpi: int = 280,
    timeout_per_page: int = 30
) -> Tuple[str, Dict[str, Any]]:
    info: Dict[str, Any] = {"ocr_lang": lang, "ocr_pages": 0, "passes": []}

    def _run_pass(_dpi: int) -> Tuple[str, Dict[str, Any]]:
        try:
            imgs = _pdf_to_images_pypdfium2(path, _dpi)
        except Exception as e:
            return "", {"error": "pdf_to_image_unavailable", "details": f"pypdfium2: {e}"}

        texts: List[str] = []
        warnings: List[str] = []
        take = min(len(imgs), max_pages)
        for i in range(take):
            try:
                # préprocess complet ici
                pim = _preprocess(imgs[i])
                t, run = ocr_image_to_text(pim, lang=lang, timeout=timeout_per_page)
                if run.get("warnings"):
                    warnings.extend(run["warnings"])
                texts.append(t or "")
            except Exception as e:
                warnings.append(f"page_{i+1}_error:{type(e).__name__}:{e}")
                texts.append("")
        full = "\n\f\n".join(texts).strip()
        meta = {"dpi": _dpi, "ocr_pages": take, "warnings": warnings}
        return full, meta

    full, meta1 = _run_pass(dpi)
    info["passes"].append(meta1)
    info["ocr_pages"] = max(info.get("ocr_pages", 0), meta1.get("ocr_pages", 0))

    if not full:
        full2, meta2 = _run_pass(320)
        info["passes"].append(meta2)
        info["ocr_pages"] = max(info.get("ocr_pages", 0), meta2.get("ocr_pages", 0))
        full = full2

    if not full:
        all_warns = []
        for p in info.get("passes", []):
            all_warns.extend(p.get("warnings", []))
        info.update({
            "error": "pdf_ocr_empty",
            "details": "no_text_after_two_passes",
            "warnings": all_warns
        })
        return "", info

    return full, info

__all__ = [
    "DEFAULT_TIMEOUT",
    "_load_image",
    "_preprocess",
    "_tesseract_try",
    "ocr_image_to_text",
    "pdf_text",
    "pdf_ocr_text",
]
