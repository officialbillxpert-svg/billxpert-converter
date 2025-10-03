from __future__ import annotations
from typing import Optional, Tuple

def _infer_totals(total_ttc: Optional[float],
                  total_ht: Optional[float],
                  total_tva: Optional[float],
                  vat_rate: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Reason about totals. Uses vat_rate when present, otherwise tries to infer consistent trio.
    """
    if vat_rate:
        rate = float(str(vat_rate).replace(',', '.')) / 100.0
    else:
        rate = None

    ht, tva, ttc = total_ht, total_tva, total_ttc

    try:
        if rate is not None:
            if ttc is not None and (ht is None or tva is None):
                ht_calc = round(ttc / (1.0 + rate), 2)
                tva_calc = round(ttc - ht_calc, 2)
                if ht is None:  ht = ht_calc
                if tva is None: tva = tva_calc

            if ht is not None and (ttc is None or tva is None):
                tva_calc = round(ht * rate, 2)
                ttc_calc = round(ht + tva_calc, 2)
                if tva is None: tva = tva_calc
                if ttc is None: ttc = ttc_calc

        # Last resort: if two are known, compute the third
        if ttc is not None and tva is not None and ht is None:
            ht = round(ttc - tva, 2)
        if ttc is not None and ht is not None and tva is None:
            tva = round(ttc - ht, 2)
        if ht is not None and tva is not None and ttc is None:
            ttc = round(ht + tva, 2)
    except Exception:
        pass

    return ht, tva, ttc
