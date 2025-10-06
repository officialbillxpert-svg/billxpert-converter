# app/extractors/ex_totals_from_lines.py
from __future__ import annotations
from typing import Dict, List
from .candidates import Cand

def ex_totals_from_lines(doc: Dict[str, any]) -> List[Cand]:
    rows = doc.get("lines") or []
    if not rows:
        return []
    s = 0.0
    for r in rows:
        try:
            amt = float(str(r.get("amount","")).replace(" ","").replace(",","."))
        except Exception:
            amt = 0.0
        s += amt
    if s <= 0:
        return []
    return [Cand(field="total_ht", value=round(s,2), conf=0.65, source="table")]
