# app/extractors/candidates.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

BBox = Tuple[int, float, float, float, float]  # (page, x0,y0,x1,y1)

@dataclass
class Cand:
    field: str                 # "seller", "buyer", "total_ttc", "invoice_date", ...
    value: Any
    conf: float                # 0..1
    source: str                # "regex", "label-prox", "ner", "xpos", "table", ...
    bbox: Optional[BBox] = None
    meta: Dict[str, Any] = field(default_factory=dict)
