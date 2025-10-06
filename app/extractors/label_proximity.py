# app/extractors/label_proximity.py
from __future__ import annotations
import re
from typing import Dict, List
from .candidates import Cand

LABELS = {
    "seller":  [r"\b(Émetteur|Emetteur|Vendeur|Seller|From)\b"],
    "buyer":   [r"\b(Client|Acheteur|Buyer|Destinataire|To)\b"],
    "total_ht":[r"\bTotal\s*[HNM][T1]\b"],
    "total_ttc":[r"\bTotal\s*(?:T[TC]C?|TT[C€]|à\s*payer)\b"],
    "total_tva":[r"\bTVA\b"],
}

def ex_label_proximity(doc: Dict[str, any]) -> List[Cand]:
    text = doc.get("text") or ""
    lines = [l for l in text.splitlines() if l.strip()]
    cands: List[Cand] = []

    def near_value(idx: int, max_ahead: int = 2):
        buf = " ".join(lines[idx: idx+1+max_ahead])
        # capture montant
        m = re.search(r"([0-9][0-9\.\,\s]+)\s*€?", buf)
        return m.group(1) if m else None

    for i, line in enumerate(lines):
        low = line.lower()
        # seller/buyer blocs (pas des montants)
        if re.search(LABELS["seller"][0], line, re.I):
            chunk = " ".join(lines[i+1:i+5]).strip()
            if chunk:
                cands.append(Cand("seller", chunk[:220], 0.7, "label-prox"))
        if re.search(LABELS["buyer"][0], line, re.I):
            chunk = " ".join(lines[i+1:i+5]).strip()
            if chunk:
                cands.append(Cand("buyer", chunk[:220], 0.7, "label-prox"))

        # montants
        if re.search(LABELS["total_ht"][0], line, re.I):
            v = near_value(i, 2)
            if v:
                cands.append(Cand("total_ht", v, 0.75, "label-prox"))
        if re.search(LABELS["total_ttc"][0], line, re.I):
            v = near_value(i, 2)
            if v:
                cands.append(Cand("total_ttc", v, 0.8, "label-prox"))
        if re.search(LABELS["total_tva"][0], line, re.I):
            v = near_value(i, 2)
            if v:
                cands.append(Cand("total_tva", v, 0.7, "label-prox"))

    return cands
