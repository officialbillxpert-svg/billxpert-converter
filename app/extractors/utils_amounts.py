from __future__ import annotations
import re
from typing import Optional

def norm_amount(s: str) -> Optional[float]:
    """Normalise un montant texte -> float (€, espaces, ,/.). Ignore IBAN/longs IDs."""
    if not s:
        return None
    s = s.strip()

    # Longue séquence de chiffres sans séparateur décimal et sans €
    digits_only = re.sub(r'\D', '', s)
    if (len(digits_only) >= 11) and ('€' not in s) and (',' not in s) and ('.' not in s):
        return None

    # IBAN-like pattern
    if re.search(r'\b\d{4}\s\d{4}\s\d{4}\s\d{4}', s):
        return None

    s = s.replace(' ', '')
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


def clean_block(s: str) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None


def approx(a: Optional[float], b: Optional[float], tol: float = 1.2) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def smart_fix_scale(t: Optional[float], ht: Optional[float], tva: Optional[float]) -> Optional[float]:
    """
    Corrige un TTC aberrant (ex: 624000 au lieu de 6240.00 à cause d’un OCR ‘,00’ perdu).
    Si /100 rapproche fortement HT+TVA et TTC, on garde la valeur corrigée.
    """
    if t is None:
        return None
    if (ht is None) and (tva is None):
        return t

    def close(x: float, y: float) -> bool:
        return abs(x - y) <= max(1.5, 0.01 * max(x, y))

    target = None
    if (ht is not None) and (tva is not None):
        target = ht + tva

    # seuil: TTC très grand et entier → candidat à /100
    if t >= 20000 and float(int(t)) == t:
        t2 = round(t / 100.0, 2)
        if target is None:
            # si HT existe, comparer (ht * 1.3 approx) pour décider
            if ht is not None and t2 >= ht and t2 <= ht * 2:
                return t2
        else:
            if close(t2, target):
                return t2
    return t
