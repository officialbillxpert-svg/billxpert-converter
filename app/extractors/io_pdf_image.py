# app/extractors/io_pdf_image.py
from __future__ import annotations

from typing import Any, Dict, Tuple, Optional, Union
from pathlib import Path
import io

from PIL import Image, ImageOps, ImageFilter
import pytesseract

# Timeout par défaut (secondes) pour Tesseract
DEFAULT_TIMEOUT: int = 30


def _load_image(path: Path) -> Image.Image:
    """Charge une image de façon robuste (gère les fichiers verrouillés ou gros)."""
    with open(path, "rb") as f:
        data = f.read()
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _preprocess(img: Image.Image) -> Image.Image:
    """
    Prétraitement léger mais robuste (sans OpenCV) :
      1) Conversion niveaux de gris
      2) Auto-contraste (clip 2%)
      3) Lissage léger (Median 3)
      4) Equalize (améliore la lisibilité)
      5) Redimensionnement max 2400 px (upsample <= 1.5x)
    """
    # Convertit RGBA (transparence) -> fond blanc
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg.convert("RGB")

    # 1) Grayscale
    g = img.convert("L")

    # 2) Auto-contraste
    g = ImageOps.autocontrast(g, cutoff=2)

    # 3) Lissage
    g = g.filter(ImageFilter.MedianFilter(size=3))

    # 4) Equalize (binarisation douce)
    g = ImageOps.equalize(g)

    # 5) Redimensionnement
    max_dim = 2400
    w, h = g.size
    scale = min(max_dim / max(w, h), 1.5)
    if scale != 1.0:
        g = g.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    return g


def _tesseract_try(
    img: Image.Image,
    lang: str,
    config: str,
    timeout_s: int
) -> Tuple[str, Dict[str, Any]]:
    """Lance Tesseract une fois avec un profil/config donné."""
    txt = pytesseract.image_to_string(img, lang=lang, config=config, timeout=timeout_s)
    info: Dict[str, Any] = {
        "ocr_lang": lang,
        "tesseract_config": config,
        "timeout_s": timeout_s,
    }
    return txt, info


def ocr_image_to_text(
    path_or_image: Union[Path, str, Image.Image],
    lang: str = "fra+eng",
    timeout_s: Optional[int] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    OCR d’une image avec plusieurs stratégies de secours.
    IMPORTANT: ne JETTE PAS d’exception ; retourne (txt, info).
      - En cas d’échec, txt="" et info contient "error" + "details".
    """
    timeout = int(timeout_s or DEFAULT_TIMEOUT)
    info: Dict[str, Any] = {"ocr_lang": lang, "ocr_used": True}

    # 1) Chargement
    try:
        if isinstance(path_or_image, Image.Image):
            img = path_or_image
        else:
            img = _load_image(Path(path_or_image))
    except Exception as e:
        info.update({
            "error": "load_image_failed",
            "details": f"{type(e).__name__}: {e}",
            "ocr_used": False,
        })
        return "", info

    # 2) Prétraitement
    try:
        pim = _preprocess(img)
    except Exception as e:
        pim = img
        info.setdefault("warnings", []).append(f"preprocess_failed:{type(e).__name__}:{e}")

    # 3) Profils de fallback (du plus général au plus tolérant)
    profiles = [
        "--oem 3 --psm 6",   # LSTM, lignes
        "--oem 1 --psm 3",   # LSTM, bloc
        "--oem 3 --psm 4",   # LSTM, colonnes/variable
        "--oem 3 --psm 11",  # LSTM, texte épars
        "--oem 0 --psm 6",   # Legacy, parfois mieux sur vieilles polices
    ]

    last_err: Optional[str] = None
    for i, cfg in enumerate(profiles, start=1):
        try:
            txt, runinfo = _tesseract_try(pim, lang=lang, config=cfg, timeout_s=timeout)
            info.update(runinfo)
            txt = (txt or "").replace("\x00", "").strip()
            if txt:
                return txt, info

            # Texte vide → essai suivant
            info.setdefault("warnings", []).append(f"empty_text_profile_{i}")

        except RuntimeError as e:
            # Pytesseract remonte les timeouts en RuntimeError
            last_err = f"RuntimeError:{e}"
            info.setdefault("warnings", []).append(f"profile_{i}_runtimeerror:{e}")

            # Si timeout, on accorde UNE relance pour ce profil (+5 à +10s)
            if "timeout" in str(e).lower():
                extra_timeout = min(timeout + 10, timeout + 15)
                try:
                    txt, runinfo = _tesseract_try(pim, lang=lang, config=cfg, timeout_s=extra_timeout)
                    info.update(runinfo)
                    txt = (txt or "").replace("\x00", "").strip()
                    if txt:
                        return txt, info
                    info.setdefault("warnings", []).append(f"empty_text_profile_{i}_retry")
                except RuntimeError as e2:
                    last_err = f"RuntimeError(retry):{e2}"
                    info.setdefault("warnings", []).append(f"profile_{i}_retry_runtimeerror:{e2}")

        except Exception as e:
            last_err = f"{type(e).__name__}:{e}"
            info.setdefault("warnings", []).append(f"profile_{i}_exception:{type(e).__name__}:{e}")

    # 4) Tous les profils ont échoué / renvoyé vide
    err = "handwriting_engine_unavailable" if (last_err and "timeout" in last_err.lower()) else "ocr_failed"
    info.update({
        "error": err,
        "details": last_err or "all_profiles_failed",
        "ocr_used": False,
    })
    return "", info


def pdf_text(path: Path) -> str:
    """
    Extraction texte natif PDF (pdfminer.six).
    Si le PDF est scanné, le texte sera souvent vide (OCR à faire ailleurs).
    """
    try:
        from pdfminer.high_level import extract_text
    except Exception:
        return ""

    try:
        return extract_text(str(path)) or ""
    except Exception:
        return ""
