from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
import re as _re

from .patterns import PATTERNS_VERSION, VAT_RATE_RE, TOTAL_TTC_NEAR_RE, TOTAL_HT_NEAR_RE, TVA_AMOUNT_NEAR_RE
from .io_pdf_image import pdf_text, ocr_image_to_text
from .fields import _fill_fields_from_text
from .totals import _infer_totals
from .lines_parsers import parse_lines_by_xpos, parse_lines_extract_table, parse_lines_regex
from .utils_amounts import approx as approx_utils


# ---------- Helpers montant ----------

def _norm_amount_str(s: str) -> str:
    """Nettoie une chaîne montant en format décimal Python '1234.56'."""
    if not s:
        return s
    s = s.strip().replace("\u00A0", " ").replace("€", "")
    # supprime espaces de milliers
    s = s.replace(" ", "")
    # heuristique FR -> .
    if "," in s:
        # si virgule est la dernière séparation décimale, enlever points de milliers
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


def _search_amount(text: str, rx: _re.Pattern) -> Optional[float]:
    """Cherche une regex de montant (capt groupe 1) dans `text` et renvoie float si possible."""
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
    """
    Rattrape OCR : 'Total MT' / 'Total MI' (HT mal lu).
    """
    m = _re.search(
        r'Total\s*M[TI]\s*[:\-]?\s*[^\n\r]{0,60}?([0-9][0-9\.\,\s]+)\s*€?',
        text or "", _re.I
    )
    return _to_float(m.group(1)) if m else None


def _post_compute_totals(fields: Dict[str, Any], vat_rate: Optional[float]) -> None:
    """
    Essaie de compléter HT/TVA/TTC à partir des infos présentes.
    - Si on a lignes + TTC approximativement égal à somme lignes => HT/TVA par _infer_totals
    - Sinon, selon combinaison connue (TTC + taux ; HT + taux ; TTC + HT ; etc.)
    """
    total_ht  = fields.get("total_ht")
    total_tva = fields.get("total_tva")
    total_ttc = fields.get("total_ttc")

    # Si HT/TVA pas fiables: 0 est traité comme "manquant"
    if total_tva == 0:
        total_tva = None

    th, tv, tt = _infer_totals(total_ttc, total_ht, total_tva, vat_rate)

    # applique uniquement ce qui est calculable
    if th is not None:
        fields["total_ht"] = th
        total_ht = th
    if tv is not None:
        fields["total_tva"] = tv
        total_tva = tv
    if tt is not None:
        fields["total_ttc"] = tt
        total_ttc = tt

    # Dernière chance : si TTC & HT présents mais TVA None -> diff
    if (fields.get("total_tva") in (None, 0)
        and fields.get("total_ttc") is not None
        and fields.get("total_ht")  is not None):
        diff = round(fields["total_ttc"] - fields["total_ht"], 2)
        if 0 <= diff <= 2_000_000:
            fields["total_tva"] = diff


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

        # Normalisations simples pour labels collés/variants
        txt = _re.sub(r'(ÉMETTEUR\s*:)\s*(DESTINATAIRE\s*:)', r'\1\n\2', txt, flags=_re.I)
        txt = _re.sub(r'(FACTURE)\s*(?:N[°o]|Nº|No)\b', r'\1 N°', txt, flags=_re.I)

        result["meta"]["ocr_used"] = True
        result["meta"]["ocr_pages"] = 1
        if "ocr_lang" in info:
            result["meta"]["ocr_lang"] = info["ocr_lang"]
        if "warnings" in info:
            result["meta"]["warnings"] += info.get("warnings", [])
        result["text"] = txt[:20000]
        result["text_preview"] = txt[:2000]

        # Champs via patterns
        _fill_fields_from_text(result, txt)

        # Rattrapage montants avec nos regex directes si manquants
        if fields.get("total_ttc") is None:
            ttc = _search_amount(txt, TOTAL_TTC_NEAR_RE)
            if ttc is not None:
                fields["total_ttc"] = ttc

        if fields.get("total_ht") is None:
            ht = _search_amount(txt, TOTAL_HT_NEAR_RE)
            if ht is None:
                ht = _patch_total_ht_fuzzy(txt)  # Total MT -> HT
            if ht is not None:
                fields["total_ht"] = ht

        if fields.get("total_tva") in (None, 0):
            tva = _search_amount(txt, TVA_AMOUNT_NEAR_RE)
            if tva is not None:
                fields["total_tva"] = tva

        # Lignes (regex fallback sur OCR)
        lines = parse_lines_regex(txt)
        if lines:
            result["lines"] = lines
            result["meta"]["line_strategy"] = "regex"
            fields["lines_count"] = len(lines)

            vat_rate  = _extract_vat_rate(txt)
            total_ttc = fields.get("total_ttc")
            sum_lines = round(sum((_to_float(r.get("amount")) or 0.0) for r in lines), 2)

            # Si TTC ~= somme lignes, calculer HT/TVA via infer
            if total_ttc and sum_lines and approx_utils(sum_lines, total_ttc, tol=1.5):
                th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
                if th is not None: fields["total_ht"]  = th
                if tv is not None: fields["total_tva"] = tv
                fields["total_ttc"] = tt or total_ttc
            else:
                _post_compute_totals(fields, vat_rate)
        else:
            # Pas de lignes : tenter quand même d'inférer d'après les montants trouvés
            vat_rate = _extract_vat_rate(txt)
            _post_compute_totals(fields, vat_rate)

        return result

    # ---------- PDF TEXTE (pdfminer) ----------
    text = pdf_text(p) or ""
    result["text"] = text[:20000]
    result["text_preview"] = text[:2000]
    result["meta"]["pages"] = (text.count("\f") + 1) if text else 0

    # Champs via patterns
    _fill_fields_from_text(result, text)

    # Rattrapage montants direct si manquants
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

    # Lignes : déterminer la stratégie réellement utilisée
    lines: List[Dict[str, Any]] | None = None

    lx = parse_lines_by_xpos(str(p))
    if lx:
        lines = lx
        result["meta"]["line_strategy"] = "xpos"
    else:
        lt = parse_lines_extract_table(str(p))
        if lt:
            lines = lt
            result["meta"]["line_strategy"] = "table"
        else:
            lr = parse_lines_regex(text)
            if lr:
                lines = lr
                result["meta"]["line_strategy"] = "regex"

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        vat_rate  = _extract_vat_rate(text)
        total_ttc = fields.get("total_ttc")
        sum_lines = round(sum((_to_float(r.get("amount")) or 0.0) for r in lines), 2)

        if total_ttc and sum_lines and approx_utils(sum_lines, total_ttc, tol=1.5):
            th, tv, tt = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = th
            fields["total_tva"] = tv
            fields["total_ttc"] = tt or total_ttc
        else:
            _post_compute_totals(fields, vat_rate)
    else:
        # Pas de lignes -> tenter quand même d’inférer les totaux
        vat_rate = _extract_vat_rate(text)
        _post_compute_totals(fields, vat_rate)

    return result
