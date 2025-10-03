from __future__ import annotations
from typing import List, Dict, Any
import re

# on importe seulement ce qui est stable
from .patterns import TABLE_HEADER_HINTS, FOOTER_NOISE_PAT

# Fallback local (au cas où)
LINE_RX_FALLBACK = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

def parse_lines_regex(text: str) -> List[Dict[str, Any]]:
    """
    Parsing simple par regex (fallback). Ne dépend pas de pdfminer.
    """
    if not text:
        return []
    # tenter d’utiliser LINE_RX défini dans patterns, sinon fallback local
    try:
        from .patterns import LINE_RX as PAT_LINE_RX  # import tardif = safe
        rx = PAT_LINE_RX or LINE_RX_FALLBACK
    except Exception:
        rx = LINE_RX_FALLBACK

    rows: List[Dict[str, Any]] = []
    for m in rx.finditer(text):
        ref   = (m.group("ref") or "").strip()
        label = (m.group("label") or "").strip()
        qty   = _to_float(m.group("qty"))
        pu    = _norm_amount(m.group("pu"))
        amt   = _norm_amount(m.group("amt"))
        rows.append({
            "ref": ref or None,
            "label": label or None,
            "qty": qty,
            "unit_price": pu,
            "amount": amt,
        })
    # filtre le bruit de pied de page
    rows = [r for r in rows if not _looks_like_footer(r.get("label") or "")]
    return rows

def parse_lines_by_xpos(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Si tu as une parse par positions X (pdfminer layout), garde-la ici.
    Pour l’instant, on renvoie [] pour éviter les crash sur PDFs scannés.
    """
    return []

def parse_lines_extract_table(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Si tu as un extracteur de tableau (camelot/tabula), mets-le ici.
    Par défaut, désactivé (évite dépendances système).
    """
    return []

# ---------- utils locaux ----------
_AMT_RX = re.compile(r'[^\d,.\-]')

def _to_float(x) -> float | None:
    try:
        return float(x)
    except Exception:
        return None

def _norm_amount(s: str | None) -> float | None:
    if not s:
        return None
    # supprime symboles/espaces, normalise séparateurs
    t = _AMT_RX.sub('', s).strip().replace(' ', '')
    # gestion "1 234,56" / "1.234,56" / "1234.56"
    if t.count(',') == 1 and t.count('.') >= 1:
        # cas "1.234,56" -> remplace '.' (milliers) par '' puis ',' -> '.'
        t = t.replace('.', '').replace(',', '.')
    elif t.count(',') == 1 and t.count('.') == 0:
        # cas "1234,56"
        t = t.replace(',', '.')
    try:
        return round(float(t), 2)
    except Exception:
        return None

def _looks_like_footer(label: str) -> bool:
    return bool(FOOTER_NOISE_PAT.search(label or ""))
