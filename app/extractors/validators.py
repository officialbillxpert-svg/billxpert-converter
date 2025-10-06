# app/extractors/validators.py
import re
from typing import Any, Optional

def soft_validate(field: str, value: Any) -> float:
    """Renvoie un multiplicateur 0..1 (qualité) selon le champ."""
    if value in (None, ""):
        return 0.0
    s = str(value)

    if field in ("total_ht","total_tva","total_ttc"):
        try:
            v = float(s)
            return 1.0 if -1e-6 <= v <= 5_000_000 else 0.6
        except Exception:
            return 0.0

    if field == "invoice_date":
        # très simple: yyyy-mm-dd dedans ?
        return 0.9 if re.search(r"\b\d{4}-\d{2}-\d{2}\b", s) else 0.6

    if field == "seller_iban":
        return 1.0 if re.search(r"\bFR\d{12,}\b", s.replace(" ","")) else 0.7

    if field == "seller_siret":
        return 1.0 if re.search(r"\b\d{9,14}\b", s) else 0.6

    if field in ("seller","buyer"):
        # au moins quelques lettres + chiffres (adresse)
        return 0.9 if len(s.strip()) >= 8 else 0.5

    return 0.8  # défaut
