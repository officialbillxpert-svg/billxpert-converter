from __future__ import annotations
import re

def _norm_amount(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    # rejeter des longues suites de chiffres (SIRET, tel, etc.) sans séparateur décimal ni €
    digits_only = re.sub(r'\D', '', s)
    if (len(digits_only) >= 11) and ('€' not in s) and (',' not in s) and ('.' not in s):
        return None
    # rejeter des blocs type IBAN
    if re.search(r'\b\d{4}\s\d{4}\s\d{4}\s\d{4}', s):
        return None
    s = s.replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        val = float(s)
    except Exception:
        return None
    if val < 0 or val > 2_000_000:
        return None
    return round(val, 2)

def _clean_block(s: str | None) -> str | None:
    import re
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None

def approx(a: float | None, b: float | None, *, tol: float = 1.2) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol
