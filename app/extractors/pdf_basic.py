# app/extractors/pdf_basic.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Optional
import re as _re

from .patterns import PATTERNS_VERSION, VAT_RATE_RE
from .io_pdf_image import pdf_text, ocr_image_to_text, pdf_ocr_text
from .fields import _fill_fields_from_text
from .utils_amounts import _norm_amount

def _looks_like_invoice_text(t: str) -> bool:
    t_low = (t or "").lower()
    markers = ["facture", "invoice", "total", "tva", "montant", "ttc", "€"]
    if any(m in t_low for m in markers):
        return True
    if _re.search(r"\b\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{2})\b", t or ""):
        return True
    return False

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

def extract_document(path: str, ocr: str = "auto") -> Dict[str, Any]:
    p = Path(path)
    ext = p.suffix.lower()
    result: Dict[str, Any] = {"meta": {"version": PATTERNS_VERSION, "source": str(p)}, "fields": {}}

    text = ""
    info = {}

    if ext == ".pdf":
        txt, info = pdf_text(p)
        text = txt or ""
        if ocr in ("force", "pdf_ocr") or (ocr == "auto" and not _looks_like_invoice_text(text)):
            txt2, info2 = pdf_ocr_text(p)
            if txt2:
                text = txt2
                info = {**info, **{"ocr": info2}}
    elif ext in (".png", ".jpg", ".jpeg"):
        txt, info = ocr_image_to_text(p)
        text = txt or ""
    else:
        result["meta"]["warning"] = f"unsupported_ext:{ext}"
        return result

    result["meta"]["io_info"] = info
    fields = _fill_fields_from_text(text or "")
    result["fields"] = fields

    vat_rate = _extract_vat_rate(text or "")
    _post_compute_totals(fields, vat_rate)

    result["meta"].setdefault("hints", {})["parties_strategy"] = "labels" if (fields.get("seller") or fields.get("buyer")) else "fallback"

    return result
