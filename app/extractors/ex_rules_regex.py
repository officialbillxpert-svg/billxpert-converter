# app/extractors/ex_rules_regex.py
from __future__ import annotations
from typing import Dict, List
from .candidates import Cand

def ex_rules_regex(doc: Dict[str, any]) -> List[Cand]:
    """
    doc contient:
      - "text": texte courant (pdfminer ou OCR)
      - "raw_fields": dict rempli par _fill_fields_from_text (si tu veux le garder)
    Ici, on convertit raw_fields -> liste de Cand
    """
    text = doc.get("text") or ""
    raw = doc.get("raw_fields") or {}
    cands: List[Cand] = []

    for k in ["invoice_number","invoice_date","total_ht","total_tva","total_ttc",
              "currency","seller","buyer","seller_siret","seller_tva","seller_iban"]:
        v = raw.get(k)
        if v is not None:
            cands.append(Cand(field=k, value=v, conf=0.9, source="regex"))

    return cands
