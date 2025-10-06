# app/extractors/orchestrator.py
from __future__ import annotations
from typing import Any, Dict, List, Tuple
from .candidates import Cand
from .validators import soft_validate

# ---- interfaces d’extracteurs (on les branchera ensuite)
def ex_rules_regex(doc: Dict[str, Any]) -> List[Cand]: ...
def ex_label_proximity(doc: Dict[str, Any]) -> List[Cand]: ...
def ex_ner_spacy(doc: Dict[str, Any]) -> List[Cand]: ...
def ex_totals_from_lines(doc: Dict[str, Any]) -> List[Cand]: ...

SOURCE_WEIGHTS = {
    "regex":         1.00,
    "label-prox":    0.95,
    "xpos":          0.95,
    "table":         0.95,
    "ner":           0.90,
}

def _weigh(c: Cand) -> float:
    w = SOURCE_WEIGHTS.get(c.source, 0.85)
    return max(0.0, min(1.0, c.conf * w * soft_validate(c.field, c.value)))

def run_extractors(doc: Dict[str, Any]) -> List[Cand]:
    cands: List[Cand] = []
    # appel en parallèle (simplement séquentiel pour l’instant)
    cands += ex_rules_regex(doc)
    cands += ex_label_proximity(doc)
    # cands += ex_ner_spacy(doc)        # activera en phase 2
    cands += ex_totals_from_lines(doc)
    # pondère les confiances
    for c in cands:
        c.conf = _weigh(c)
    return cands

def resolve_fields(cands: List[Cand]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    # regroupe par champ
    by_field: Dict[str, List[Cand]] = {}
    for c in cands:
        by_field.setdefault(c.field, []).append(c)

    final: Dict[str, Any] = {}
    confs: Dict[str, Any] = {}

    for field, lst in by_field.items():
        lst.sort(key=lambda x: x.conf, reverse=True)
        if not lst:
            continue
        top = lst[0]
        final[field] = top.value
        confs[field] = {
            "value": top.value,
            "conf": round(top.conf, 3),
            "source": top.source,
            "alts": [
                {"value": a.value, "conf": round(a.conf,3), "source": a.source}
                for a in lst[1:3]
            ]
        }

    # contraintes globales simples (TTC ≈ HT+TVA)
    ht = _to_float_safe(final.get("total_ht"))
    tva = _to_float_safe(final.get("total_tva"))
    ttc = _to_float_safe(final.get("total_ttc"))
    if ht is not None and tva is not None and ttc is None:
        final["total_ttc"] = round(ht + tva, 2)
    if ht is not None and ttc is not None and tva is None:
        diff = round(ttc - ht, 2)
        if 0 <= diff <= 2_000_000:
            final["total_tva"] = diff

    return final, confs

def _to_float_safe(v):
    try:
        return float(str(v).replace(" ","").replace(",","."))
    except Exception:
        return None
