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
    """Nettoie une chaîne montant en format décimal Python '1234.56'."""
    if not s:
        return s
    s = s.strip().replace("\u00A0", " ").replace("€", "")
    s = s.replace(" ", "")  # supprime espaces de milliers
    # heuristique FR -> '.' si virgule est la dernière séparation décimale
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
    """Accepte déjà float/int ou str et renvoie float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return _to_float(str(v))


def _search_amount(text: str, rx: _re.Pattern) -> Optional[float]:
    """Cherche une regex de montant (capt groupe 1) et renvoie float si possible."""
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
    """Rattrape OCR : 'Total MT' / 'Total MI' (HT mal lu)."""
    m = _re.search(
        r'Total\s*M[TI]\s*[:\-]?\s*[^\n\r]{0,60}?([0-9][0-9\.\,\s]+)\s*€?',
        text or "", _re.I
    )
    return _to_float(m.group(1)) if m else None


def _post_compute_totals(fields: Dict[str, Any], vat_rate: Optional[float]) -> None:
    """
    Essaie de compléter HT/TVA/TTC à partir des infos présentes.
    - Tolère total_tva == 0 comme “manquant”
    - Utilise _infer_totals selon combinaisons disponibles
    - Termine par TVA = TTC - HT si cohérent
    """
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

    # Dernière chance cohérente : TVA = TTC - HT si possible
    if (
        fields.get("total_tva") in (None, 0)
        and fields.get("total_ttc") is not None
        and fields.get("total_ht")  is not None
    ):
        diff = round(fields["total_ttc"] - fields["total_ht"], 2)
        if 0 <= diff <= 2_000_000:
            fields["total_tva"] = diff


# ---------- Heuristique “ça ressemble à une facture ?” ----------

def _looks_like_invoice_text(t: str) -> bool:
    """Heuristique rapide pour détecter du texte 'facture' plausible."""
    t_low = (t or "").lower()
    markers = [
        "facture", "invoice", "total", "tva", "montant", "pu", "qté", "ttc", "t.t.c", "€"
    ]
    if any(m in t_low for m in markers):
        return True
    # Nombres style 1 234,56 ou 1.234,56
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

        # Rattrapage montants si manquants
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
            sum_lines = round(sum((_to_num(r.get("amount")) or 0.0) for r in lines), 2)

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

    # On tente d’abord d’extraire les champs sur le texte pdfminer…
    _fill_fields_from_text(result, text)

    # Décide si on doit basculer en OCR :
    # - texte trop court
    # - texte qui ne "ressemble pas" à une facture
    # - aucun champ clé trouvé (numéro/date/HT/TVA/TTC)
    empty_core = not any([
        result["fields"].get("invoice_number"),
        result["fields"].get("invoice_date"),
        result["fields"].get("total_ht"),
        result["fields"].get("total_tva"),
        result["fields"].get("total_ttc"),
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
            # petites normalisations utiles
            ocr_txt = _re.sub(r'(ÉMETTEUR\s*:)\s*(DESTINATAIRE\s*:)', r'\1\n\2', ocr_txt, flags=_re.I)
            ocr_txt = _re.sub(r'(FACTURE)\s*(?:N[°o]|Nº|No)\b', r'\1 N°', ocr_txt, flags=_re.I)

            result["meta"]["ocr_used"] = True
            result["meta"]["ocr_pages"] = oinfo.get("ocr_pages") or 0
            if oinfo.get("warnings"):
                result["meta"]["warnings"] += oinfo["warnings"]

            text = ocr_txt  # -> on bascule le pipeline sur l’OCR
            result["text"] = text[:20000]
            result["text_preview"] = text[:2000]
            ocr_used = True

            # re-parse des champs sur le texte OCR
            _fill_fields_from_text(result, text)
        else:
            # OCR a essayé mais rien d’exploitable
            if oinfo.get("error"):
                result["meta"]["warnings"].append(
                    f"pdf_ocr:{oinfo.get('error')}:{oinfo.get('details','')}".strip()
                )

    # --- Rattrapage montants si manquants (marche pour pdfminer OU OCR) ---
    if result["fields"].get("total_ttc") is None:
        ttc = _search_amount(text, TOTAL_TTC_NEAR_RE)
        if ttc is not None:
            result["fields"]["total_ttc"] = ttc

    if result["fields"].get("total_ht") is None:
        ht = _search_amount(text, TOTAL_HT_NEAR_RE)
        if ht is None:
            ht = _patch_total_ht_fuzzy(text)
        if ht is not None:
            result["fields"]["total_ht"] = ht

    if result["fields"].get("total_tva") in (None, 0):
        tva = _search_amount(text, TVA_AMOUNT_NEAR_RE)
        if tva is not None:
            result["fields"]["total_tva"] = tva

    # --- Lignes : déterminer la stratégie réellement utilisée ---
    lines: List[Dict[str, Any]] | None = None

    if not ocr_used:
        # On tente les stratégies 'xpos' / 'table' sur le PDF original uniquement
        lx = parse_lines_by_xpos(str(p))
        if lx:
            lines = lx
            result["meta"]["line_strategy"] = "xpos"
        else:
            lt = parse_lines_extract_table(str(p))
            if lt:
                lines = lt
                result["meta"]["line_strategy"] = "table"

    # Si toujours rien, tente une regex sur le texte courant (pdfminer ou OCR)
    if not lines:
        lr = parse_lines_regex(text)
        if lr:
            lines = lr
            result["meta"]["line_strategy"] = "regex"

    # --- Totaux à partir des lignes (si présentes) ---
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
        # Pas de lignes -> tenter quand même d’inférer les totaux
        vat_rate = _extract_vat_rate(text)
        _post_compute_totals(fields, vat_rate)

    return result
