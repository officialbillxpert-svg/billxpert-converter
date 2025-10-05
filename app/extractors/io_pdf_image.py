from __future__ import annotations
from typing import Tuple, Dict, Any, Optional
from pathlib import Path
from PIL import Image, ImageOps, ImageFilter
import io, pytesseract

DEFAULT_TIMEOUT = 25

def _load_image(path: Path) -> Image.Image:
    with open(path, "rb") as f:
        img = Image.open(io.BytesIO(f.read()))
        img.load()
    return img

def _preprocess(img: Image.Image) -> Image.Image:
    g = img.convert("L")
    g = ImageOps.autocontrast(g, cutoff=2)
    g = g.filter(ImageFilter.MedianFilter(size=3))
    g = ImageOps.equalize(g)
    w, h = g.size
    max_dim = 2400
    scale = min(max_dim / max(w, h), 1.5)
    if scale != 1.0:
        g = g.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
    return g

def _tesseract_try(img: Image.Image, lang: str, cfg: str, timeout: int) -> Tuple[str, Dict[str, Any]]:
    txt = pytesseract.image_to_string(img, lang=lang, config=cfg, timeout=timeout)
    return txt.replace("\x00", "").strip(), {"ocr_lang": lang, "tesseract_config": cfg, "timeout_s": timeout}

def ocr_image_to_text(path_or_image: Path | Image.Image, lang: str = "fra+eng", timeout: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    timeout = timeout or DEFAULT_TIMEOUT
    try:
        img = path_or_image if isinstance(path_or_image, Image.Image) else _load_image(Path(path_or_image))
    except Exception as e:
        return "", {"error": "load_image_failed", "details": str(e)}

    warnings = []
    try:
        pim = _preprocess(img)
    except Exception as e:
        pim = img
        warnings.append(f"preprocess_failed:{e}")

    profiles = ["--oem 3 --psm 6","--oem 1 --psm 3","--oem 3 --psm 4","--oem 3 --psm 11","--oem 0 --psm 6"]
    last_err = None
    for cfg in profiles:
        try:
            txt, info = _tesseract_try(pim, lang, cfg, timeout)
            info["warnings"] = warnings
            if txt:
                return txt, info
            warnings.append(f"empty_text:{cfg}")
        except RuntimeError as e:
            last_err = f"RuntimeError:{e}"
            if "timeout" in str(e).lower():
                try:
                    txt, info = _tesseract_try(pim, lang, cfg, min(timeout+5, timeout+10))
                    info["warnings"] = warnings
                    if txt:
                        return txt, info
                except RuntimeError as e2:
                    last_err = f"RuntimeError(retry):{e2}"
        except Exception as e:
            last_err = f"{type(e).__name__}:{e}"

    return "", {
        "error": "handwriting_engine_unavailable" if last_err and "timeout" in last_err.lower() else "ocr_failed",
        "details": last_err or "all_profiles_failed",
        "ocr_lang": lang,
        "ocr_used": False,
        "warnings": warnings,
    }

def pdf_text(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception:
        return ""

# ---- PDF -> OCR par pages (fallback pour PDF scannés) ----
def pdf_ocr_text(path: Path, lang: str = "fra+eng", max_pages: int = 5, dpi: int = 220, timeout_per_page: int = 25) -> Tuple[str, Dict[str, Any]]:
    """
    Rasterise jusqu'à 'max_pages' du PDF avec pypdfium2 puis applique Tesseract
    page par page. Renvoie (texte_concatené, infos).
    """
    info: Dict[str, Any] = {"ocr_lang": lang, "ocr_pages": 0}
    try:
        import pypdfium2 as pdfium
    except Exception as e:
        info.update({"error": "pdf_to_image_unavailable", "details": f"pypdfium2 import failed: {e}"})
        return "", info

    try:
        pdf = pdfium.PdfDocument(str(path))
        n_pages = len(pdf)
        take = min(n_pages, max_pages)
        texts: list[str] = []
        for i in range(take):
            page = pdf[i]
            pil = page.render(scale=dpi / 72.0).to_pil()
            t, run = ocr_image_to_text(pil, lang=lang, timeout=timeout_per_page)
            if run.get("warnings"):
                info.setdefault("warnings", []).extend(run["warnings"])
            texts.append(t or "")
        info["ocr_pages"] = take
        return ("\n\f\n".join(texts)).strip(), info
    except Exception as e:
        info.update({"error": "pdf_ocr_failed", "details": f"{type(e).__name__}: {e}"})
        return "", info
