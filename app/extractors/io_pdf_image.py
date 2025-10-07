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


def _pil_basic_preprocess(img: Image.Image) -> Image.Image:
    """
    Prétraitement léger et robuste (PIL only) :
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


def _cv2_binarize_if_available(img: Image.Image) -> Image.Image:
    """
    Si OpenCV est dispo, applique une binarisation/adaptive threshold
    souvent très utile pour les scans pâles.
    """
    try:
        import numpy as np
        import cv2

        arr = np.array(img.convert("L"))
        # légère égalisation puis adaptive threshold
        arr = cv2.equalizeHist(arr)
        thr = cv2.adaptiveThreshold(arr, 255,
                                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 35, 11)
        return Image.fromarray(thr)
    except Exception:
        # OpenCV non installé ou erreur : on retourne l'image d'origine
        return img


def _preprocess(img: Image.Image) -> Image.Image:
    """
    Pipeline de prétraitement :
      - PIL basique
      - puis tentative OpenCV (si dispo)
    """
    g = _pil_basic_preprocess(img)
    g = _cv2_binarize_if_available(g)
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
    Essaie fra+eng puis fallback en 'eng' si les data FR ne sont pas présentes.
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
                # pytesseract lève RuntimeError sur timeout
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

    # Première passe avec la langue demandée (fra+eng par défaut)
    txt, info = _run_profiles(pim, lang)
    # Si erreur due aux data langue manquantes, on retente en 'eng'
    if (not txt) and isinstance(info, dict) and ("details" in info):
        det = str(info.get("details") or "").lower()
        if "failed loading language" in det or "not available" in det or "tesseract couldn't load any languages" in det:
            warnings.append("lang_missing_fallback_to_eng")
            txt2, info2 = _run_profiles(pim, "eng")
            if txt2:
                return txt2, info2

    return txt, info


# ---------- Extraction texte natif PDF (pdfminer.six + fallback pdfplumber) ----------

def pdf_text(path: Path) -> str:
    """
    Tente d'extraire le texte natif d’un PDF (non scanné).
    1) pdfminer.six
    2) fallback pdfplumber (souvent meilleur sur certaines mises en page)
    Renvoie '' en cas d'erreur.
    """
    # Pass 1 : pdfminer
    try:
        from pdfminer.high_level import extract_text
        t = extract_text(str(path)) or ""
        if t and len(t.strip()) > 30:
            return t
    except Exception:
        pass

    # Pass 2 : pdfplumber
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


# ---------- OCR des PDF scannés (pypdfium2 + Tesseract) ----------

def _pdf_to_images_pypdfium2(path: Path, dpi: int) -> List[Image.Image]:
    """Rend les pages PDF (jusqu’à max_pages côté appelant) en images PIL via pypdfium2."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        images: List[Image.Image] = []
        for i in range(len(pdf)):
            page = pdf[i]
            pil = page.render(scale=dpi / 72.0).to_pil()
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
    dpi: int = 220,
    timeout_per_page: int = 25
) -> Tuple[str, Dict[str, Any]]:
    """
    Rasterise jusqu'à 'max_pages' avec pypdfium2, applique Tesseract page par page.
    Double passe DPI : 220 puis 300 si rien n’est lu.
    Retourne (texte_concaténé, info). N'échoue pas en exception.
    """
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
                t, run = ocr_image_to_text(imgs[i], lang=lang, timeout=timeout_per_page)
                if run.get("warnings"):
                    warnings.extend(run["warnings"])
                texts.append(t or "")
            except Exception as e:
                warnings.append(f"page_{i+1}_error:{type(e).__name__}:{e}")
                texts.append("")
        full = "\n\f\n".join(texts).strip()
        meta = {"dpi": _dpi, "ocr_pages": take, "warnings": warnings}
        return full, meta

    # 1ère passe
    full, meta1 = _run_pass(dpi)
    info["passes"].append(meta1)
    info["ocr_pages"] = max(info.get("ocr_pages", 0), meta1.get("ocr_pages", 0))

    if not full:
        # 2ème passe plus précise
        full2, meta2 = _run_pass(300)
        info["passes"].append(meta2)
        info["ocr_pages"] = max(info.get("ocr_pages", 0), meta2.get("ocr_pages", 0))
        full = full2

    if not full:
        # Aggrège les warnings lisibles
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
