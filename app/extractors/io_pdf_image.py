from __future__ import annotations
from pathlib import Path
from typing import Tuple, Dict, Union

from pdfminer.high_level import extract_text as _pdfminer_extract_text

try:
    import pytesseract
    from PIL import Image, ImageOps, UnidentifiedImageError  # type: ignore
except Exception:
    pytesseract = None
    Image = None
    ImageOps = None
    UnidentifiedImageError = Exception

def pdf_text(path: Union[str, Path]) -> str:
    try:
        return _pdfminer_extract_text(str(path)) or ""
    except Exception:
        return ""

def ocr_image_to_text(path: Union[str, Path], lang: str = "fra+eng") -> Tuple[str, Dict[str, str]]:
    if pytesseract is None or Image is None or ImageOps is None:
        return "", {"error": "tesseract_not_found", "details": "Binaire tesseract ou Pillow manquant."}
    try:
        import shutil
        tpath = shutil.which("tesseract")
        if tpath:
            pytesseract.pytesseract.tesseract_cmd = tpath
    except Exception:
        pass

    def preprocess(img: "Image.Image") -> "Image.Image":
        g = ImageOps.grayscale(img)
        return g.point(lambda x: 255 if x > 180 else 0, mode="1")

    tried, last_err = [], None
    for l in [lang, "eng"]:
        if not l:
            continue
        tried.append(l)
        try:
            img = Image.open(str(path))
            img = preprocess(img)
            txt = pytesseract.image_to_string(img, lang=l) or ""
            if txt.strip():
                return txt, {"ocr_lang": l}
        except UnidentifiedImageError as e:
            return "", {"error": "bad_image", "details": f"UnidentifiedImageError: {e}"}
        except pytesseract.TesseractNotFoundError:  # type: ignore
            return "", {"error": "tesseract_not_found", "details": "Binaire tesseract absent."}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return "", {"error": "ocr_failed", "details": last_err or "OCR vide.", "tried_langs": tried}
