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
    """
    Charge une image depuis disque (binaire) et force le .load() pour éviter
    des lazy-reads sur certains drivers.
    """
    with open(path, "rb") as f:
        img = Image.open(io.BytesIO(f.read()))
        img.load()
    return img


def _preprocess(img: Image.Image) -> Image.Image:
    """
    Prétraitement léger et robuste :
      - niveaux de gris
      - autocontrast (clip 2%)
      - lissage (median 3)
      - equalize
      - resize max 2400 px (upsample <= 1.5x)
    """
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


def _tesseract_try(img: Image.Image, lang: str, cfg: str, timeout: int) -> Tuple[str, Dict[str, Any]]:
    """
    Lance Tesseract avec un profil donné et renvoie (texte, info).
    """
    txt = pytesseract.image_to_string(img, lang=lang, config=cfg, timeout=timeout)
    txt = txt.replace("\x00", "").strip()
    return txt, {"ocr_lang": lang, "tesseract_config": cfg, "timeout_s": timeout}


# ---------- OCR Image unique (PNG/JPG) ----------

def ocr_image_to_text(
    path_or_image: Path | Image.Image,
    lang: str = "fra+eng",
    timeout: Optional[int] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    OCR d'une image (ou d'un objet PIL Image) avec plusieurs profils de secours.
    Ne lève pas d'exception : renvoie ('', info{error=...}) en cas d'échec.
    """
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
        "--oem 3 --psm 6",   # LSTM + lignes
        "--oem 1 --psm 3",   # LSTM + bloc
        "--oem 3 --psm 4",   # LSTM + colonnes
        "--oem 3 --psm 11",  # sparse
        "--oem 0 --psm 6",   # Legacy engine
    ]

    last_err: Optional[str] = None
    for cfg in profiles:
        try:
            txt, info = _tesseract_try(pim, lang, cfg, timeout)
            info["warnings"] = warnings
            if txt:
                return txt, info
            warnings.append(f"empty_text:{cfg}")
        except RuntimeError as e:
            # pytesseract lève RuntimeError sur timeout
            last_err = f"RuntimeError:{e}"
            if "timeout" in str(e).lower():
                # une relance courte
                try:
                    txt, info = _tesseract_try(pim, lang, cfg, min(timeout + 5, timeout + 10))
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


# ---------- Extraction texte natif PDF (pdfminer.six) ----------

def pdf_text(path: Path) -> str:
    """
    Tente d'extraire le texte natif d’un PDF (non scanné).
    Renvoie '' en cas d'erreur.
    """
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception:
        return ""


# ---------- OCR des PDF scannés (pypdfium2 + Tesseract) ----------

def pdf_ocr_text(
    path: Path,
    lang: str = "fra+eng",
    max_pages: int = 5,
    dpi: int = 220,
    timeout_per_page: int = 25
) -> Tuple[str, Dict[str, Any]]:
    """
    Rasterise jusqu'à 'max_pages' avec pypdfium2, applique Tesseract page par page.
    Retourne (texte_concaténé, info). N'échoue pas en exception.
    """
    info: Dict[str, Any] = {"ocr_lang": lang, "ocr_pages": 0}
    try:
        import pypdfium2 as pdfium
    except Exception as e:
        info.update({
            "error": "pdf_to_image_unavailable",
            "details": f"pypdfium2 import failed: {e}"
        })
        return "", info

    pdf = None
    try:
        pdf = pdfium.PdfDocument(str(path))
        n_pages = len(pdf)
        take = min(n_pages, max_pages)
        texts: List[str] = []

        for i in range(take):
            try:
                page = pdf[i]
                # 72 dpi = base PDF ; scale pour viser 'dpi'
                pil = page.render(scale=dpi / 72.0).to_pil()
                # On réutilise le pipeline image -> OCR (prétraitement inclus)
                t, run = ocr_image_to_text(pil, lang=lang, timeout=timeout_per_page)
                if run.get("warnings"):
                    info.setdefault("warnings", []).extend(run["warnings"])
                texts.append(t or "")
            except RuntimeError as e:
                info.setdefault("warnings", []).append(f"page_{i+1}_runtime:{e}")
                texts.append("")
            except Exception as e:
                info.setdefault("warnings", []).append(f"page_{i+1}_error:{type(e).__name__}:{e}")
                texts.append("")

        info["ocr_pages"] = take
        full = "\n\f\n".join(texts).strip()
        if not full:
            info.setdefault("warnings", []).append("pdf_ocr_empty")
            return "", info
        return full, info

    except Exception as e:
        info.update({"error": "pdf_ocr_failed", "details": f"{type(e).__name__}: {e}"})
        return "", info
    finally:
        # pypdfium2 n'exige pas forcément close(), mais on garde une fermeture prudente
        try:
            if pdf is not None:
                pdf.close()
        except Exception:
            pass


__all__ = [
    "DEFAULT_TIMEOUT",
    "_load_image",
    "_preprocess",
    "_tesseract_try",
    "ocr_image_to_text",
    "pdf_text",
    "pdf_ocr_text",
]
