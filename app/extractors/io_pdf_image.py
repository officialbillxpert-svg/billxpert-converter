from __future__ import annotations
import io
import os
from pathlib import Path
from typing import Dict, Tuple, Optional

import pytesseract
from PIL import Image, ImageOps, ImageFilter

# Timeout par défaut (secondes) – adapté aux images A4 300 dpi
DEFAULT_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "20"))

def _load_image(path: Path) -> Image.Image:
    with open(path, "rb") as f:
        img = Image.open(io.BytesIO(f.read()))
        img.load()
    return img

def _preprocess(img: Image.Image) -> Image.Image:
    """
    Prétraitement robuste mais léger (sans OpenCV):
    - Convertit en niveaux de gris
    - Auto-contraste
    - Lissage léger pour bruit / compression
    - Binarisation douce
    - Redimensionnement max dim 2400 px
    """
    # 1) Grayscale
    g = img.convert("L")

    # 2) Auto-contraste (clip 2% pour éviter de cramer)
    g = ImageOps.autocontrast(g, cutoff=2)

    # 3) Lissage léger (évite faux contours JPEG)
    g = g.filter(ImageFilter.MedianFilter(size=3))

    # 4) Binarisation douce (point de seuil adaptatif simple)
    #    On garde du niveau de gris si besoin pour OCR, donc on ne durcit pas trop
    #    => on applique un “stretch” doux
    g = ImageOps.equalize(g, mask=None)

    # 5) Redimensionnement (si trop petit ou trop grand)
    max_dim = 2400
    w, h = g.size
    scale = min(max_dim / max(w, h), 1.5)  # on upsample max x1.5
    if scale != 1.0:
        g = g.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    return g

def _tesseract_try(img: Image.Image, lang: str, config: str, timeout: int) -> Tuple[str, Dict]:
    """
    Lance Tesseract une fois avec un profil donné.
    Renvoie (texte, info).
    """
    text = pytesseract.image_to_string(img, lang=lang, config=config, timeout=timeout)
    info = {
        "ocr_lang": lang,
        "tesseract_config": config,
        "timeout_s": timeout,
    }
    return text, info

def ocr_image_to_text(path_or_image: Path | Image.Image, lang: str = "fra+eng", timeout: Optional[int] = None) -> Tuple[str, Dict]:
    """
    OCR d’une image avec plusieurs stratégies de secours.
    Renvoie (texte, info). En cas d’échec, lève RuntimeError avec un dict “info” exploitable.
    """
    timeout = timeout or DEFAULT_TIMEOUT
    info: Dict = {}

    try:
        img = path_or_image if isinstance(path_or_image, Image.Image) else _load_image(Path(path_or_image))
    except Exception as e:
        info.update({"error": f"load_image_failed:{type(e).__name__}", "details": str(e)})
        raise RuntimeError("Image load failed")  # laisser pdf_basic capturer proprement

    # Prétraitement
    try:
        pim = _preprocess(img)
    except Exception as e:
        # On tente quand même l’original si le prétraitement a échoué
        pim = img
        info.setdefault("warnings", []).append(f"preprocess_failed:{type(e).__name__}:{e}")

    # Profils de fallback : du plus “général” au plus “tolérant”
    profiles = [
        "--oem 3 --psm 6",   # LSTM + mode semi-automatique (lignes de texte)
        "--oem 1 --psm 3",   # LSTM + bloc de texte
        "--oem 3 --psm 4",   # LSTM + colonnes / texte variable
        "--oem 3 --psm 11",  # Sparse text (épars)
        "--oem 0 --psm 6",   # Legacy engine (parfois mieux sur police très “sales”)
    ]

    last_err: Optional[str] = None
    for i, cfg in enumerate(profiles, start=1):
        try:
            txt, runinfo = _tesseract_try(pim, lang=lang, config=cfg, timeout=timeout)
            info.update(runinfo)
            # Nettoyage simple (couper les null bytes, normaliser fin de ligne)
            txt = txt.replace("\x00", "").strip()
            if txt:
                return txt, info
            # Texte vide : on tente le profil suivant
            info.setdefault("warnings", []).append(f"empty_text_profile_{i}")
        except RuntimeError as e:
            # Timeout ou autre erreur tesseract levée par pytesseract
            last_err = f"RuntimeError:{e}"
            info.setdefault("warnings", []).append(f"profile_{i}_runtimeerror:{e}")
            # Si timeout, on accorde UNE relance avec +5s
            if "timeout" in str(e).lower():
                extra_timeout = min(timeout + 5, timeout + 10)
                try:
                    txt, runinfo = _tesseract_try(pim, lang=lang, config=cfg, timeout=extra_timeout)
                    info.update(runinfo)
                    txt = txt.replace("\x00", "").strip()
                    if txt:
                        return txt, info
                    info.setdefault("warnings", []).append(f"empty_text_profile_{i}_retry")
                except RuntimeError as e2:
                    last_err = f"RuntimeError(retry):{e2}"
                    info.setdefault("warnings", []).append(f"profile_{i}_retry_runtimeerror:{e2}")
            # On enchaîne sur le profil suivant
        except Exception as e:
            last_err = f"{type(e).__name__}:{e}"
            info.setdefault("warnings", []).append(f"profile_{i}_exception:{type(e).__name__}:{e}")

    # Si on est là : tous les profils ont échoué / donné vide
    err = "handwriting_engine_unavailable" if ("timeout" in (last_err or "").lower()) else "ocr_failed"
    info.update({
        "error": err,
        "details": last_err or "all_profiles_failed",
        "ocr_lang": lang,
        "ocr_used": False,
    })
    raise RuntimeError(err)

def pdf_text(path: Path) -> str:
    """
    Extraction texte natif PDF (pdfminer.six).
    Si le PDF est une image scannée, cette fonction renverra souvent peu ou pas de texte.
    L’OCR des PDFs scannés se fait ailleurs (convert-to-image puis ocr_image_to_text par page).
    """
    try:
        from pdfminer.high_level import extract_text
    except Exception:
        return ""

    try:
        return extract_text(str(path)) or ""
    except Exception:
        return ""
