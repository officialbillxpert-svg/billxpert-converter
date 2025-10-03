from __future__ import annotations
import re
from typing import Optional, List, Dict

from .patterns import TABLE_HEADER_HINTS

def norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    digits_only = re.sub(r'\D', '', s)
    if (len(digits_only) >= 11) and ('€' not in s) and (',' not in s) and ('.' not in s):
        return None
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

def approx(a: Optional[float], b: Optional[float], tol: float = 1.2) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol

def clean_block(s: str) -> Optional[str]:
    import re as _re
    s = _re.sub(r'\s+', ' ', s or '').strip()
    return s or None

def norm_header_cell(s: str) -> str:
    import re as _re
    s = (s or "").strip().lower()
    s = (s.replace("é","e").replace("è","e").replace("ê","e")
           .replace("à","a").replace("û","u").replace("ï","i"))
    s = s.replace("\n"," ").replace("\t"," ")
    s = _re.sub(r"\s+"," ", s)
    return s

def map_header_indices(headers: List[str]) -> Optional[Dict[str, int]]:
    idx: Dict[str, Optional[int]] = {}
    norm = [norm_header_cell(h) for h in headers]
    def match_one(*cands):
        for i, h in enumerate(norm):
            for c in cands:
                if c in h:
                    return i
        return None
    idx["ref"]    = match_one(*TABLE_HEADER_HINTS[0])
    idx["label"]  = match_one(*TABLE_HEADER_HINTS[1])
    idx["qty"]    = match_one(*TABLE_HEADER_HINTS[2])
    idx["unit"]   = match_one(*TABLE_HEADER_HINTS[3])
    idx["amount"] = match_one(*TABLE_HEADER_HINTS[4])
    if all(v is None for v in idx.values()):
        return None
    return {k: v for k, v in idx.items() if v is not None}
