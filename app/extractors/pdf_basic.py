from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .patterns import PATTERNS_VERSION
from .io_pdf_image import pdf_text, ocr_image_to_text
from .fields import fill_fields_from_text, extract_vat_rate
from .lines_parsers import parse_lines_by_xpos, parse_lines_extract_table, parse_lines_regex
from .utils_amounts import approx
from .totals import infer_totals

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

    # --- images -> OCR ---
    if ext in {".png", ".jpg", ".jpeg"}:
        txt, info = ocr_image_to_text(p, lang="fra+eng")
        if info.get("error"):
            result["success"] = False
            result.update(info)
            return result

        result["meta"]["ocr_used"] = True
        result["meta"]["ocr_pages"] = 1
        if "ocr_lang" in info: result["meta"]["ocr_lang"] = info["ocr_lang"]

        result["text"] = txt[:20000]
        result["text_preview"] = txt[:2000]

        fill_fields_from_text(result, txt)

        lines = parse_lines_regex(txt)
        if lines:
            result["lines"] = lines
            result["meta"]["line_strategy"] = "regex"
            fields["lines_count"] = len(lines)

            vat_rate  = extract_vat_rate(txt)
            total_ttc = fields.get("total_ttc")
            sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2)

            if total_ttc and sum_lines and approx(sum_lines, total_ttc, tol=1.5):
                th, tv, tt = infer_totals(total_ttc, None, None, vat_rate)
                if th is not None: fields["total_ht"]  = th
                if tv is not None: fields["total_tva"] = tv
                fields["total_ttc"] = tt or total_ttc
            else:
                total_ht = sum_lines if sum_lines else fields.get("total_ht")
                th, tv, tt = infer_totals(total_ttc, total_ht, fields.get("total_tva"), vat_rate)
                if th is not None: fields["total_ht"]  = th
                if tv is not None: fields["total_tva"] = tv
                if tt is not None: fields["total_ttc"] = tt

        # post-pass cohérence TVA
        if (fields.get("total_tva") is None 
            and fields.get("total_ttc") is not None 
            and fields.get("total_ht")  is not None):
            diff = round(fields["total_ttc"] - fields["total_ht"], 2)
            if 0 <= diff <= 2_000_000:
                fields["total_tva"] = diff

        return result

    # --- PDF ---
    text = pdf_text(p) or ""
    result["text"] = text[:20000]
    result["text_preview"] = text[:2000]
    result["meta"]["pages"] = (text.count("\f") + 1) if text else 0

    fill_fields_from_text(result, text)

    lines = parse_lines_by_xpos(str(p))
    if lines:
        result["lines"] = lines
        result["meta"]["line_strategy"] = "xpos"
    else:
        lines = parse_lines_extract_table(str(p))
        if lines:
            result["lines"] = lines
            result["meta"]["line_strategy"] = "table"
        else:
            lines = parse_lines_regex(text)
            if lines:
                result["lines"] = lines
                result["meta"]["line_strategy"] = "regex"

    if lines:
        fields["lines_count"] = len(lines)
        vat_rate  = extract_vat_rate(text)
        total_ttc = fields.get("total_ttc")
        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2)

        if total_ttc and sum_lines and approx(sum_lines, total_ttc, tol=1.5):
            th, tv, tt = infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = th
            fields["total_tva"] = tv
            fields["total_ttc"] = tt or total_ttc
        else:
            total_ht = sum_lines if sum_lines else fields.get("total_ht")
            th, tv, tt = infer_totals(total_ttc, total_ht, fields.get("total_tva"), vat_rate)
            if th is not None: fields["total_ht"]  = th
            if tv is not None: fields["total_tva"] = tv
            if tt is not None: fields["total_ttc"] = tt

    if (fields.get("total_tva") is None 
        and fields.get("total_ttc") is not None 
        and fields.get("total_ht")  is not None):
        diff = round(fields["total_ttc"] - fields["total_ht"], 2)
        if 0 <= diff <= 2_000_000:
            fields["total_tva"] = diff

    return result
