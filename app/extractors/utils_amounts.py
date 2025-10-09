
from __future__ import annotations

def _norm_amount(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace("€","").replace("\u00A0"," ").replace(" ","")
    # if comma as decimal sep and dot as thousands
    if "," in s and s.count(",") == 1 and "." in s:
        s = s.replace(".","").replace(",",".")
    elif "," in s and s.count(",") == 1:
        s = s.replace(",",".")
    try:
        return float(s)
    except Exception:
        return None

def _clean_block(block: str | None, max_lines: int = 6) -> str | None:
    if not block:
        return None
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    return "\\n".join(lines[:max_lines]).strip() or None
