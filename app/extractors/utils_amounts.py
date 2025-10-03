# app/extractors/utils_amounts.py
from __future__ import annotations
import re
from typing import Optional

def _norm_amount(s: str) -> Optional[float]:
    """Normalise un montant. Ignore IBAN/numéros/absurdités."""
    if not s:
        return None
    s = s.strip()

    # Longue séquence de chiffres sans séparateur décimal et sans €
    digits_only = re.sub(r'\D', '', s)
    if (len(digits_only) >= 11) and ('€' not in s) and (',' not in s) and ('.' not in s):
        return None

    # IBAN-like
    if re.search(r'\b\d{4}\s\d{4}\s\d{4}\s\d{4}', s):
        return None

    s = s.replace(' ', '')
    # "1.234,56" -> "1234.56"
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')

    try:
        val = float(s)
        if val < 0 or val > 2_000_000:
            return None
        return round(val, 2)
    except Exception:
        return None


def _clean_block(s: str) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None


def approx(a: Optional[float], b: Optional[float], tol: float = 1.2) -> bool:
    """Petit helper optionnel exporté (certain code l'importe)."""
    if a is None or b is None:
        return False
    return abs(a - b) <= tol
