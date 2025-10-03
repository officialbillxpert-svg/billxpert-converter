from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

from .patterns import LINE_RX, FOOTER_NOISE_PAT
from .utils_amounts import norm_amount, norm_header_cell, map_header_indices

# pdfplumber optionnel
try:
    import pdfplumber
except Exception:
    pdfplumber = None  # type: ignore

def parse_lines_regex(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in LINE_RX.finditer(text or ""):
        qty = int(m.group('qty'))
        pu  = norm_amount(m.group('pu'))
        amt = norm_amount(m.group('amt'))
        if FOOTER_NOISE_PAT.search((m.group('label') or '') + ' ' + (m.group('ref') or '')):
            continue
        rows.append({
            "ref":        m.group('ref'),
            "label":      m.group('label').strip(),
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    return rows

def parse_lines_by_xpos(pdf_path: str) -> List[Dict[str, Any]]:
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
                if not words:
                    continue
                lines_by_y = {}
                for w in words:
                    mid_y = int((w["top"] + w["bottom"]) / 2)
                    lines_by_y.setdefault(mid_y, []).append(w)

                # header
                header_y, header_cells = None, {}
                def norm(s: str) -> str: return norm_header_cell(s)
                from .patterns import TABLE_HEADER_HINTS
                for yk, ws in sorted(lines_by_y.items(), key=lambda kv: kv[0]):
                    score, hitmap = 0, {}
                    for w in ws:
                        t = norm(w["text"])
                        if any(c in t for c in TABLE_HEADER_HINTS[2]): score += 1; hitmap["qty"]   = w
                        if any(c in t for c in TABLE_HEADER_HINTS[3]): score += 1; hitmap["unit"]  = w
                        if any(c in t for c in TABLE_HEADER_HINTS[4]): score += 1; hitmap["amount"]= w
                        if any(c in t for c in TABLE_HEADER_HINTS[1]): score += 1; hitmap["label"] = w
                        if any(c in t for c in TABLE_HEADER_HINTS[0]): score += 1; hitmap["ref"]   = w
                    if score >= 3:
                        header_y, header_cells = yk, hitmap
                        break
                if header_y is None:
                    continue

                # fin de tableau (ligne "total")
                total_y = None
                for yk, ws in sorted(lines_by_y.items(), key=lambda kv: kv[0]):
                    if yk <= header_y:
                        continue
                    txt = " ".join(norm(w["text"]) for w in ws)
                    if "total" in txt:
                        total_y = yk
                        break

                cols = []
                for role in ["ref", "label", "qty", "unit", "amount"]:
                    if role in header_cells:
                        w = header_cells[role]
                        cols.append((role, (w["x0"] + w["x1"]) / 2))
                cols = sorted(cols, key=lambda t: t[1])
                if not cols:
                    continue

                col_bounds: List[Tuple[str, float, float]] = []
                for i, (role, xmid) in enumerate(cols):
                    if i == 0:
                        left = 0.0
                        right = (cols[i+1][1] + xmid) / 2 if i+1 < len(cols) else xmid + 9999
                    elif i == len(cols) - 1:
                        left = (cols[i-1][1] + xmid) / 2
                        right = 999999.0
                    else:
                        left = (cols[i-1][1] + xmid) / 2
                        right = (cols[i+1][1] + xmid) / 2
                    col_bounds.append((role, left, right))

                def in_body(yk: int) -> bool:
                    if yk <= header_y + 5: return False
                    if total_y is not None and yk >= total_y - 5: return False
                    return True

                # regrouper bandes
                bands: List[Tuple[int, List[dict]]] = []
                for yk in sorted(lines_by_y.keys()):
                    if not in_body(yk): continue
                    ws = sorted(lines_by_y[yk], key=lambda w: w["x0"])
                    if not bands: bands.append((yk, ws))
                    else:
                        last_y, last_ws = bands[-1]
                        if abs(yk - last_y) <= 6: last_ws.extend(ws)
                        else: bands.append((yk, ws))

                from .patterns import FOOTER_NOISE_PAT
                import re
                for _, ws in bands:
                    full_text = " ".join(w["text"] for w in ws)
                    if FOOTER_NOISE_PAT.search(full_text): continue
                    cells = {role: [] for (role, _, _) in col_bounds}
                    for w in ws:
                        xmid = (w["x0"] + w["x1"]) / 2
                        for role, left, right in col_bounds:
                            if left <= xmid < right:
                                cells[role].append(w["text"])
                                break
                    ref   = " ".join(cells.get("ref", [])).strip() or None
                    label = " ".join(cells.get("label", [])).strip() or None
                    qtys  = " ".join(cells.get("qty", [])).strip()
                    pu    = " ".join(cells.get("unit", [])).strip()
                    amt   = " ".join(cells.get("amount", [])).strip()
                    def _to_int(s: str) -> Optional[int]:
                        s2 = re.sub(r"[^\d]", "", s or "")
                        if not s2: return None
                        try:
                            val = int(s2)
                            if val < 0 or val > 999: return None
                            return val
                        except Exception:
                            return None
                    qty_i  = _to_int(qtys)
                    pu_f   = norm_amount(pu)
                    amt_f  = norm_amount(amt)

                    if FOOTER_NOISE_PAT.search((label or "") + " " + (ref or "")): continue
                    if not (label or pu_f is not None or amt_f is not None or qty_i is not None or ref): continue
                    if (not label) and ref: label = ref
                    if amt_f is None and (pu_f is not None) and (qty_i is not None): amt_f = round(pu_f * qty_i, 2)
                    if (qty_i is not None) and (not label) and (pu_f is None) and (amt_f is None): continue
                    if label and FOOTER_NOISE_PAT.search(label): continue

                    rows.append({
                        "ref": ref, "label": label or "", "qty": qty_i,
                        "unit_price": pu_f, "amount": amt_f
                    })

        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen: continue
            seen.add(key); uniq.append(r)
        return uniq
    except Exception:
        return []

def parse_lines_extract_table(pdf_path: str) -> List[Dict[str, Any]]:
    if pdfplumber is None:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = []
                t = page.extract_table()
                if t: tables.append(t)
                t2 = page.extract_table({"vertical_strategy":"lines", "horizontal_strategy":"lines"})
                if t2: tables.append(t2)
                from .utils_amounts import map_header_indices
                from .patterns import FOOTER_NOISE_PAT
                for tbl in tables:
                    tbl = [[(c or "").strip() for c in (row or [])] for row in (tbl or []) if any((row or []))]
                    if not tbl or len(tbl) < 2: continue
                    header = tbl[0]
                    idx = map_header_indices(header)
                    if not idx: continue
                    import re
                    for line in tbl[1:]:
                        def get(i): return line[i] if (i is not None and i < len(line)) else ""
                        ref, label = get(idx.get("ref")), get(idx.get("label"))
                        qty, pu, amt = get(idx.get("qty")), get(idx.get("unit")), get(idx.get("amount"))
                        try:
                            qty_i = int(re.sub(r"[^\d]", "", qty)) if qty else None
                            if qty_i is not None and (qty_i < 0 or qty_i > 999): qty_i = None
                        except Exception:
                            qty_i = None
                        from .utils_amounts import norm_amount
                        pu_f, amt_f = norm_amount(pu), norm_amount(amt)
                        if amt_f is None and pu_f is not None and qty_i is not None: amt_f = round(pu_f * qty_i, 2)
                        if not (label or pu_f is not None or amt_f is not None or qty_i is not None): continue
                        if FOOTER_NOISE_PAT.search((label or "") + " " + (ref or "")): continue
                        rows.append({
                            "ref": (ref or "").strip() or None,
                            "label": (label or ref or "").strip(),
                            "qty": qty_i,
                            "unit_price": pu_f,
                            "amount": amt_f
                        })
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen: continue
            seen.add(key); uniq.append(r)
        return uniq
    except Exception:
        return []
