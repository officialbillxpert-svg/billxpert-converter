# app/extractors/pdf_basic.py
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
        r'Total\s*M[TI]\s*[:\-]?\s*[^\n\r]{0,80}?([0-9][0-9\.\,\s]+)\s*€?',
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
        total_ht = th
    if tv is not None:
        fields["total_tva"] = tv
        total_tva = tv
    if tt is not None:
        fields["total_ttc"] = tt
        total_ttc = tt

    if (
        fields.get("total_tva") in (None, 0)
        and fields.get("total_ttc") is not None
        and fields.get("total_ht")  is not None
    ):
        diff = round(fields["total_ttc"] - fields["total_ht"], 2)
        if 0 <= diff <= 2_000_000:
            fields["total_tva"] = diff


# ---------- Nettoyage et extraction parties (seller/buyer) ----------

_META_NOISE_RX = _re.compile(r"^file://|capture d['’]écran|^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}", _re.I)
_STOP_LABELS_RX = _re.compile(
    r"(?:^|\b)(Description|Désignation|Prix|Unitaire|PU|Qté|Montant|TOTAL|TTC|TVA|RÈGLEMENT|REMIS|IBAN|Conditions?)\b",
    _re.I
)
_SELLER_LABEL_RX = _re.compile(r"(?:^|\s)(?:ÉMETTEUR|EMETTEUR|Émetteur|Emetteur|Vendeur|Seller|From)\s*:?\s*$", _re.I)
_BUYER_LABEL_RX  = _re.compile(r"(?:^|\s)(?:DESTINATAIRE|Client|Acheteur|Buyer|To)\s*:?\s*$", _re.I)

def _pre_clean_text(t: str) -> str:
    """Supprime lignes méta (file://, Capture d’écran...), compresse espaces."""
    if not t:
        return t
    out = []
    for line in (t or "").splitlines():
        if _META_NOISE_RX.search(line or ""):
            continue
        out.append(line.replace("\u00A0", " ").strip())
    return "\n".join(out)

def _first_nonempty(lines: List[str], start: int) -> int:
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i

def _block_after_label(lines: List[str], label_idx: int, max_lines: int = 5) -> str:
    """Prend 1..max_lines lignes après un label, stop si un label/section connue arrive."""
    i = _first_nonempty(lines, label_idx + 1)
    collected: List[str] = []
    for k in range(i, min(i + 12, len(lines))):
        line = lines[k].strip()
        if not line:
            if collected:
                break
            else:
                continue
        if _STOP_LABELS_RX.search(line):
            break
        if _SELLER_LABEL_RX.search(line) or _BUYER_LABEL_RX.search(line):
            break
        collected.append(line)
        if len(collected) >= max_lines:
            break
    return "\n".join(collected).strip()

def _fix_parties_from_labels(text: str, fields: Dict[str, Any]) -> None:
    """Si seller/buyer manquants ou suspects, tente une extraction par proximité de labels."""
    lines = [l for l in (text or "").splitlines()]
    seller_block = None
    buyer_block  = None

    for idx, line in enumerate(lines):
        if _SELLER_LABEL_RX.search(line):
            blk = _block_after_label(lines, idx, max_lines=6)
            if blk and len(blk) >= 6:
                seller_block = blk
                break

    for idx, line in enumerate(lines):
        if _BUYER_LABEL_RX.search(line):
            blk = _block_after_label(lines, idx, max_lines=6)
            if blk and len(blk) >= 6:
                buyer_block = blk
                break

    def _is_label_only(x: Optional[str]) -> bool:
        if not x:
            return True
        s = x.strip().lower()
        return s in ("destinataire:", "émetteur:", "emetteur:", "seller:", "buyer:", "client:", "acheteur:")

    if (not fields.get("seller")) or _is_label_only(fields.get("seller")):
        if seller_block:
            fields["seller"] = seller_block
    if (not fields.get("buyer")) or _is_label_only(fields.get("buyer")):
        if buyer_block:
            fields["buyer"] = buyer_block

    if fields.get("buyer") and _STOP_LABELS_RX.search(fields["buyer"]):
        chunk = []
        for l in fields["buyer"].splitlines():
            if _STOP_LABELS_RX.search(l):
                break
            chunk.append(l)
        cleaned = "\n".join(chunk).strip()
        if len(cleaned) >= 6:
            fields["buyer"] = cleaned
        else:
            fields["buyer"] = None


# ---------- Heuristique “ça ressemble à une facture ?” ----------

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

    # ---------- IMAGES -> OCR ----------
    if ext in {".png", ".jpg", ".jpeg"}:
        txt, info = ocr_image_to_text(p, lang="fra+eng")
        if info.get("error"):
            result["success"] = False
            result["error"] = info.get("error")
            result["details"] = info.get("details")
            result["meta"]["ocr_used"] = False
            result["meta"]["ocr_pages"] = 0
            return result

        # normalisations OCR
        txt = _re.sub(r'(ÉMETTEUR\s*:)\s*(DESTINATAIRE\s*:)', r'\1\n\2', txt, flags=_re.I)
        txt = _re.sub(r'(FACTURE)\s*(?:N[°o]|Nº|No)\b', r'\1 N°', txt, flags=_re.I)
        txt = _pre_clean_text(txt)

        result["meta"]["ocr_used"] = True
        result["meta"]["ocr_pages"] = 1
        if "ocr_lang" in info:
            result["meta"]["ocr_lang"] = info["ocr_lang"]
        if "warnings" in info:
            result["meta"]["warnings"] += info.get("warnings", [])
        result["text"] = txt[:20000]
        result["text_preview"] = txt[:2000]

        _fill_fields_from_text(result, txt)
        _fix_parties_from_labels(txt, fields)

        # rattrapage montants
        if fields.get("total_ttc") is None:
            ttc = _search_amount(txt, TOTAL_TTC_NEAR_RE)
            if ttc is not None:
                fields["total_ttc"] = ttc

        if fields.get("total_ht") is None:
            ht = _search_amount(txt, TOTAL_HT_NEAR_RE)
            if ht is None:
                ht = _patch_total_ht_fuzzy(txt)
            if ht is not None:
                fields["total_ht"] = ht

        if fields.get("total_tva") in (None, 0):
            tva = _search_amount(txt, TVA_AMOUNT_NEAR_RE)
            if tva is not None:
                fields["total_tva"] = tva

        # lignes (regex fallback sur OCR)
        lines = parse_lines_regex(txt)
        if lines:
            result["lines"] = lines
            result["meta"]["line_strategy"] = "regex"
            fields["lines_count"] = len(lines)

            vat_rate  = _extract_vat_rate(txt)
            total_ttc = fields.get("total_ttc")
            sum_lines = round(sum((_to_num(r.get("amount")) or 0.0) for r in lines), 2)

            if total_ttc and sum_lines and approx_utils(sum_lines, total_ttc, tol=1.5):
                th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
                if th is not None: fields["total_ht"]  = th
                if tv is not None: fields["total_tva"] = tv
                fields["total_ttc"] = tt or total_ttc
            else:
                _post_compute_totals(fields, vat_rate)
        else:
            vat_rate = _extract_vat_rate(txt)
            _post_compute_totals(fields, vat_rate)

        # hint parties
        (result["meta"].setdefault("hints", {}))["parties_strategy"] = "ocr_blocks_labels"
        return result

    # ---------- PDF TEXTE (pdfminer) ----------
    text_raw = pdf_text(p) or ""
    text = _pre_clean_text(text_raw)
    result["text"] = text[:20000]
    result["text_preview"] = text[:2000]
    result["meta"]["pages"] = (text_raw.count("\f") + 1) if text_raw else 0

    _fill_fields_from_text(result, text)
    _fix_parties_from_labels(text, fields)

    empty_core = not any([
        fields.get("invoice_number"),
        fields.get("invoice_date"),
        fields.get("total_ht"),
        fields.get("total_tva"),
        fields.get("total_ttc"),
    ])
    need_ocr_fallback = (len(result["text"]) < 120) or (not _looks_like_invoice_text(result["text"])) or empty_core
    ocr_used = False
    if ocr in ("always", "force"):
        need_ocr_fallback = True

    if need_ocr_fallback:
        ocr_txt, oinfo = pdf_ocr_text(
            p, lang="fra+eng", max_pages=5, dpi=280, timeout_per_page=30
        )
        if ocr_txt:
            ocr_txt = _re.sub(r'(ÉMETTEUR\s*:)\s*(DESTINATAIRE\s*:)', r'\1\n\2', ocr_txt, flags=_re.I)
            ocr_txt = _re.sub(r'(FACTURE)\s*(?:N[°o]|Nº|No)\b', r'\1 N°', ocr_txt, flags=_re.I)
            ocr_txt = _pre_clean_text(ocr_txt)

            result["meta"]["ocr_used"] = True
            result["meta"]["ocr_pages"] = oinfo.get("ocr_pages") or 0
            if oinfo.get("warnings"):
                result["meta"]["warnings"] += oinfo["warnings"]

            text = ocr_txt
            result["text"] = text[:20000]
            result["text_preview"] = text[:2000]
            ocr_used = True

            _fill_fields_from_text(result, text)
            _fix_parties_from_labels(text, fields)

            (result["meta"].setdefault("hints", {}))["ocr_trigger"] = "short_or_unconvincing_or_empty_core"
        else:
            if oinfo.get("error"):
                result["meta"]["warnings"].append(
                    f"pdf_ocr:{oinfo.get('error')}:{oinfo.get('details','')}".strip()
                )

    # Rattrapage montants (pdfminer ou OCR)
    if fields.get("total_ttc") is None:
        ttc = _search_amount(text, TOTAL_TTC_NEAR_RE)
        if ttc is not None:
            fields["total_ttc"] = ttc

    if fields.get("total_ht") is None:
        ht = _search_amount(text, TOTAL_HT_NEAR_RE)
        if ht is None:
            ht = _patch_total_ht_fuzzy(text)
        if ht is not None:
            fields["total_ht"] = ht

    if fields.get("total_tva") in (None, 0):
        tva = _search_amount(text, TVA_AMOUNT_NEAR_RE)
        if tva is not None:
            fields["total_tva"] = tva

    # Lignes
    lines: List[Dict[str, Any]] | None = None

    if not ocr_used:
        lx = parse_lines_by_xpos(str(p))
        if lx:
            lines = lx
            result["meta"]["line_strategy"] = "xpos"
        else:
            lt = parse_lines_extract_table(str(p))
            if lt:
                lines = lt
                result["meta"]["line_strategy"] = "table"

    if not lines:
        lr = parse_lines_regex(text)
        if lr:
            lines = lr
            result["meta"]["line_strategy"] = "regex"

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        vat_rate  = _extract_vat_rate(text)
        total_ttc = fields.get("total_ttc")
        sum_lines = round(sum((_to_num(r.get("amount")) or 0.0) for r in lines), 2)

        if total_ttc and sum_lines and approx_utils(sum_lines, total_ttc, tol=1.5):
            th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = th
            fields["total_tva"] = tv
            fields["total_ttc"] = tt or total_ttc
        else:
            _post_compute_totals(fields, vat_rate)
    else:
        vat_rate = _extract_vat_rate(text)
        _post_compute_totals(fields, vat_rate)

    # hint parties (quel chemin a permis d'avoir des parties)
    (result["meta"].setdefault("hints", {}))["parties_strategy"] = (
        "blocks"
        if (fields.get("seller") and fields.get("buyer"))
        else "labels"
        if (fields.get("seller") or fields.get("buyer"))
        else "header_fallback"
    )

    return result
