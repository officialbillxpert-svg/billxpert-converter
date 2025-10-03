from __future__ import annotations
import re
from typing import Optional

# Guards
_IBAN_LIKE = re.compile(r'\b\d{4}\s\d{4}\s\d{4}\s\d{4}')
_LONG_DIGITS = re.compile(r'^\d{11,}$')

def _norm_amount(s: str) -> Optional[float]:
    """Normalize amounts and drop obvious noise (IBAN, long digit blobs)."""
    if not s:
        return None
    s0 = s.strip()

    if _LONG_DIGITS.match(re.sub(r'\D', '', s0)) and ('€' not in s0) and (',' not in s0) and ('.' not in s0):
        return None
    if _IBAN_LIKE.search(s0):
        return None

    s1 = s0.replace(' ', '')
    # "1.234,56" -> "1234.56", "1 234,56" -> "1234.56"
    if ',' in s1 and '.' in s1:
        s1 = s1.replace('.', '').replace(',', '.')
    elif ',' in s1:
        s1 = s1.replace(',', '.')

    try:
        val = float(s1)
    except Exception:
        return None

    # Heuristic: misplaced thousands => "624000" but the doc uses comma cents elsewhere
    # If val is very large and ends with '000', try divide by 100.
    if val >= 100000 and s1.endswith('000'):
        val2 = round(val / 100.0, 2)
        if 1000 <= val2 <= 100000:  # plausible invoice range
            val = val2

    if val < 0 or val > 2_000_000:
        return None
    return round(val, 2)

def _clean_block(s: str) -> str | None:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None

def approx(a: Optional[float], b: Optional[float], tol: float = 1.2) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol
