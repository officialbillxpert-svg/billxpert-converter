from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
import re as _re

from .patterns import (
    PATTERNS_VERSION,
    VAT_RATE_RE,
    TOTAL_TTC_NEAR_RE,
    TOTAL_HT_NEAR_RE,
    TVA_AMOUNT_NEAR_RE,
)
from .io_pdf_image import pdf_text, ocr_image_to_text, pdf_ocr_text
from .fields import _fill_fields_from_text
from .totals import _infer_totals
from .lines_parsers import (
    parse_lines_by_xpos,
    parse_lines_extract_table,
    parse_lines_regex,
)
from .utils_amounts import approx as approx_utils

# ---------- Helpers montant ----------

def _norm_amount_str(s: str) -> str:
    if not s:
        return s
    s = s.strip().replace("\u00A0", " ").replace("€", "")
    s = s.replace(" ", "")
    if "," in s:
        last_comma = s.rfind(",")
        last_dot = s.rfind(".")
        if last_comma > last_dot:
            s = s.replace(".", "")
            s = s.replace(",", ".")
    return s

def _to_float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(_norm_amount_str(s))
    except Exception:
        return None

def _to_num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return _to_float(str(v))

def _search_amount(text: str, rx: _re.Pattern) -> Optional[float]:
    m = rx.search(text or "")
    if not m:
        return None
    return _to_float(m.group(1))

def _extract_vat_rate(text: str) -> Optional[float]:
    m = VAT_RATE_RE.search(text or "")
    if not m:
        return None
    vr = m.group(1)
    return 5.5 if vr in ("5,5", "5.5") else float(vr)

def _patch_total_ht_fuzzy(text: str) -> Optional[float]:
    m = _re.search(
        r'Tota[l1]\s*M[TI7]\s*[:\-]?\s*[^\n\r]{0,80}?([0-9][0-9\.\,\s]+)\s*€?',
        text or "", _re.I
    )
    return _to_float(m.group(1)) if m else None

def _post_compute_totals(fields: Dict[str, Any], vat_rate: Optional[float]) -> None:
    total_ht  = fields.get("total_ht")
    total_tva = fields.get("total_tva")
    total_ttc = fields.get("total_ttc")

    if total_tva == 0:
        total_tva = None

    th, tv, tt = _infer_totals(total_ttc, total_ht, total_tva, vat_rate)

    if th is not None:
        fields["total_ht"] = th
    if tv is not None:
        fields["total_tva"] = tv
    if tt is not None:
        fields["total_ttc"] = tt

    if (
        fields.get("total_tva") in (None, 0)
        and fields.get("total_ttc") is not None
        and fields.get("total_ht")  is not None
    ):
        diff = round(fields["total_ttc"] - fields["total_ht"], 2)
        if 0 <= diff <= 2_000_000:
            fields["total_tva"] = diff

# ---------- Nettoyage ----------

_META_NOISE_RX = _re.compile(r"^file://|capture d['’]écran|^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}", _re.I)

def _pre_clean_text(t: str) -> str:
    """# FIX: nettoie sans supprimer les vraies lignes OCR"""
    if not t:
        return t
    out = []
    for line in (t or "").splitlines():
        if _META_NOISE_RX.search(line or "") and not _re.search(r"facture|invoice", line, _re.I):
            continue
        out.append(line.replace("\u00A0", " ").strip())
    return "\n".join(out)

# ---------- Détection facture ----------

def _looks_like_invoice_text(t: str) -> bool:
    t_low = (t or "").lower()
    markers = ["facture", "invoice", "total", "tva", "montant", "pu", "qté", "ttc", "t.t.c", "€"]
    if any(m in t_low for m in markers):
        return True
    if _re.search(r"\b\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{2})\b", t or ""):
        return True
    return False

# ---------- Extraction principale ----------

def extract_document(path: str, ocr: str = "auto") -> Dict[str, Any]:
    p = Path(path)
    ext = p.suffix.lower()

    result: Dict[str, Any] = {
        "success": True,
        "meta": {
            "bytes": p.stat().st_size if p.exists() else None,
            "filename": p.name,
            "pages": 0,
            "ocr_used": False,
            "ocr_pages": 0,
            "from_images": ext in {".png", ".jpg", ".jpeg"},
            "line_strategy": "none",
            "patterns_version": PATTERNS_VERSION,
            "warnings": [],
        },
        "fields": {
            "invoice_number": None,
            "invoice_date":   None,
            "total_ht":  None,
            "total_tva": None,
            "total_ttc": None,
            "currency":  "EUR",
        },
        "text": "",
        "text_preview": "",
    }
    fields = result["fields"]

    # ---------- IMAGE ----------
    if ext in {".png", ".jpg", ".jpeg"}:
        txt, info = ocr_image_to_text(p, lang="fra+eng")
        if info.get("error"):
            result["success"] = False
            result["error"] = info.get("error")
            result["details"] = info.get("details")
            return result

        txt = _pre_clean_text(txt)
        result["meta"]["ocr_used"] = True
        result["meta"]["ocr_pages"] = 1
        result["text"] = txt[:20000]
        result["text_preview"] = txt[:2000]
        _fill_fields_from_text(result, txt)
        return result

    # ---------- PDF ----------
    text_raw = pdf_text(p) or ""
    text = _pre_clean_text(text_raw)
    result["text"] = text[:20000]
    result["meta"]["pages"] = (text_raw.count("\f") + 1) if text_raw else 0
    _fill_fields_from_text(result, text)

    empty_core = not any([
        fields.get("invoice_number"),
        fields.get("invoice_date"),
        fields.get("total_ht"),
        fields.get("total_tva"),
        fields.get("total_ttc"),
    ])

    # FIX: déclencheur OCR plus tolérant
    if "capture d'écran" in text.lower() or "file://" in text.lower():
        text = ""
        need_ocr_fallback = True
    else:
        need_ocr_fallback = (len(text) < 300) or (not _looks_like_invoice_text(text)) or empty_core

    if ocr in ("always", "force"):
        need_ocr_fallback = True

    if need_ocr_fallback:
        ocr_txt, oinfo = pdf_ocr_text(
            p, lang="fra+eng", max_pages=5, dpi=280, timeout_per_page=30
        )
        if ocr_txt:
            ocr_txt = _pre_clean_text(ocr_txt)
            result["meta"]["ocr_used"] = True
            result["meta"]["ocr_pages"] = oinfo.get("ocr_pages") or 0
            text = ocr_txt
            result["text"] = text[:20000]
            _fill_fields_from_text(result, text)
        elif oinfo.get("error"):
            result["meta"]["warnings"].append(f"pdf_ocr:{oinfo.get('error')}:{oinfo.get('details','')}")

    # Montants manquants
    if fields.get("total_ttc") is None:
        ttc = _search_amount(text, TOTAL_TTC_NEAR_RE)
        if ttc is not None:
            fields["total_ttc"] = ttc

    if fields.get("total_ht") is None:
        ht = _search_amount(text, TOTAL_HT_NEAR_RE) or _patch_total_ht_fuzzy(text)
        if ht is not None:
            fields["total_ht"] = ht

    if fields.get("total_tva") in (None, 0):
        tva = _search_amount(text, TVA_AMOUNT_NEAR_RE)
        if tva is not None:
            fields["total_tva"] = tva

    return result
