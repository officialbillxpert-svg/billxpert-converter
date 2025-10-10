# app/extractors/pdf_basic.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Optional
import re as _re
import os

from .patterns import PATTERNS_VERSION, VAT_RATE_RE
from .io_pdf_image import (
    pdf_text, ocr_image_to_text, pdf_ocr_text,
    pdf_paddle_text, image_paddle_text
)
from .fields import _fill_fields_from_text
from .utils_amounts import _norm_amount  # si utilisé ailleurs

_AMOUNT_RE = _re.compile(r"\b\d{1,3}(?:[ .]\d{3})*(?:[.,]\d{2})\s?€?\b")

# -------------------------
# Heuristiques de bascule
# -------------------------

def _looks_like_invoice_text(t: str) -> bool:
    t_low = (t or "").lower()
    markers = ["facture", "invoice", "total", "tva", "montant", "ttc", "€"]
    if any(m in t_low for m in markers):
        return True
    if _AMOUNT_RE.search(t or ""):
        return True
    return False

def _score_fields(fields: Dict[str, Any]) -> int:
    keys = ("invoice_number", "invoice_date", "total_ht", "total_ttc", "total_tva", "seller", "buyer")
    return sum(1 for k in keys if fields.get(k))

def _should_try_ocr_after_text(plain_text: str, fields: Dict[str, Any]) -> bool:
    txt = (plain_text or "").strip()
    score = _score_fields(fields)
    amount_hits = len(_AMOUNT_RE.findall(txt))
    if len(txt) < 120:
        return True
    if score <= 1 and amount_hits < 1:
        return True
    return False

# -------------------------
# Utilitaires montants/TVA
# -------------------------

def _extract_vat_rate(text: str) -> Optional[float]:
    m = VAT_RATE_RE.search(text or "")
    if not m:
        return None
    s = m.group(1).replace("%","").replace(",",".").strip()
    try:
        return float(s)
    except Exception:
        return None

def _post_compute_totals(fields: Dict[str, Any], vat_rate: Optional[float]) -> None:
    ht, tva, ttc = fields.get("total_ht"), fields.get("total_tva"), fields.get("total_ttc")
    if ht is not None and ttc is not None and tva is None:
        fields["total_tva"] = round(float(ttc) - float(ht), 2)
    elif ht is not None and tva is not None and ttc is None:
        fields["total_ttc"] = round(float(ht) + float(tva), 2)
    elif ttc is not None and tva is not None and ht is None:
        fields["total_ht"] = round(float(ttc) - float(tva), 2)
    elif vat_rate is not None and ht is not None and fields.get("total_tva") is None:
        fields["total_tva"] = round(float(ht) * vat_rate/100.0, 2)
        fields.setdefault("total_ttc", round(float(ht) + fields["total_tva"], 2))

# -------------------------
# Extraction principale
# -------------------------

def extract_document(path: str, ocr: str = "auto", engine: str = "auto") -> Dict[str, Any]:
    """
    ocr:    "auto" | "force" | "off"
    engine: "auto" | "tesseract" | "paddle"
    """
    p = Path(path)
    ext = p.suffix.lower()
    result: Dict[str, Any] = {"meta": {"version": PATTERNS_VERSION, "source": str(p)}, "fields": {}}
    text = ""
    info: Dict[str, Any] = {}

    # -------- Images directes --------
    if ext in (".png", ".jpg", ".jpeg"):
        if engine == "paddle":
            txt, info = image_paddle_text(p)
        else:
            txt, info = ocr_image_to_text(p)  # Tesseract
        text = txt or ""
        fields = _fill_fields_from_text(text or "")
        result["fields"] = fields
        result["meta"]["io_info"] = info
        _finalize(result, text)
        return result

    # -------- PDF --------
    if ext == ".pdf":
        # 1) Passe texte rapide
        txt, info_text = pdf_text(p)
        text = txt or ""
        info.update(info_text or {})
        fields = _fill_fields_from_text(text or "")

        # 2) Décisions suivant 'engine'
        if engine == "tesseract":
            # Tesseract direct (ou fallback si off/auto)
            if ocr != "off" and (ocr == "force" or _should_try_ocr_after_text(text, fields) or not _looks_like_invoice_text(text)):
                try:
                    txt2, info2 = pdf_ocr_text(p)
                    if txt2.strip():
                        fields2 = _fill_fields_from_text(txt2)
                        if _score_fields(fields2) >= _score_fields(fields):
                            text, fields = txt2, fields2
                            info["engine"] = "ocr_tesseract"
                            info["fallback"] = "forced" if ocr == "force" else "auto_triggered"
                            info["ocr_info"] = info2
                        else:
                            info["fallback_tried"] = "ocr_tesseract"
                except Exception as e:
                    info["ocr_error"] = f"{type(e).__name__}:{e}"

        elif engine == "paddle":
            # Paddle direct
            try:
                txt2, info2 = pdf_paddle_text(p)
                if txt2.strip():
                    fields2 = _fill_fields_from_text(txt2)
                    # On préfère Paddle si score >=
                    if _score_fields(fields2) >= _score_fields(fields):
                        text, fields = txt2, fields2
                        info = {**info, **info2}
                    else:
                        info["paddle_tried"] = True
                        info["paddle_score_lt_text"] = True
                else:
                    info["paddle_error"] = "empty_text"
            except Exception as e:
                info["paddle_error"] = f"{type(e).__name__}:{e}"

        else:  # engine == "auto"
            # D'abord Tesseract si texte pauvre…
            used_any = False
            if ocr != "off" and (ocr == "force" or _should_try_ocr_after_text(text, fields) or not _looks_like_invoice_text(text)):
                try:
                    txt2, info2 = pdf_ocr_text(p)
                    if txt2.strip():
                        fields2 = _fill_fields_from_text(txt2)
                        if _score_fields(fields2) >= _score_fields(fields):
                            text, fields = txt2, fields2
                            info["engine"] = "ocr_tesseract"
                            info["fallback"] = "forced" if ocr == "force" else "auto_triggered"
                            info["ocr_info"] = info2
                            used_any = True
                        else:
                            info["fallback_tried"] = "ocr_tesseract"
                except Exception as e:
                    info["ocr_error"] = f"{type(e).__name__}:{e}"

            # … si toujours pauvre, on tente Paddle
            if not used_any and _score_fields(fields) <= 1:
                try:
                    txt3, info3 = pdf_paddle_text(p)
                    if txt3.strip():
                        fields3 = _fill_fields_from_text(txt3)
                        if _score_fields(fields3) >= _score_fields(fields):
                            text, fields = txt3, fields3
                            info = {**info, **info3, "engine": "paddleocr", "fallback": "auto_triggered"}
                        else:
                            info["paddle_tried"] = True
                    else:
                        info["paddle_error"] = "empty_text"
                except Exception as e:
                    info["paddle_error"] = f"{type(e).__name__}:{e}"

        result["fields"] = fields
        result["meta"]["io_info"] = info
        _finalize(result, text)
        return result

    # Extension inconnue
    result["meta"]["warning"] = f"unsupported_ext:{ext}"
    return result

def _finalize(result: Dict[str, Any], text: str) -> None:
    vat_rate = _extract_vat_rate(text or "")
    _post_compute_totals(result["fields"], vat_rate)
    result["meta"].setdefault("hints", {})["parties_strategy"] = (
        "labels" if (result["fields"].get("seller") or result["fields"].get("buyer")) else "fallback"
    )