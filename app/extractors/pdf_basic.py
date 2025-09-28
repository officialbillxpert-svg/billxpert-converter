# app/extractors/pdf_basic.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import re
from dateutil import parser as dateparser

# --- pdfminer pour le texte brut ---
from pdfminer.high_level import extract_text as _extract_text

# --- pdfplumber (optionnel mais recommandé) ---
try:
    import pdfplumber
except Exception:
    pdfplumber = None

# =========================
#        REGEX de base
# =========================
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

# Totaux (texte) – FR/EN
TOTAL_TTC_RE = re.compile(r'(?:^|\n)\s*(?:Total\s*(?:TTC)?|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*:?\s*([0-9][0-9\.\,\s]+)\s*$', re.I|re.M)
TOTAL_HT_RE  = re.compile(r'(?:^|\n)\s*(?:Total\s*HT|Sous-?total|Sub\s*total)\s*:?\s*([0-9][0-9\.\,\s]+)\s*$', re.I|re.M)
# Montant TVA (pas le numéro FR…)
TOTAL_TVA_RE = re.compile(r'(?:^|\n)\s*(?:TVA|VAT)\s*\(?\s*\d+(?:[.,]\d+)?%\s*\)?\s*:?\s*([0-9][0-9\.\,\s]+)\s*$', re.I|re.M)

EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR (à masquer avant la recherche des montants)
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_ID_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')  # identifiant TVA FRxx#########
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# Lignes fallback (texte)
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Taux TVA
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%?', re.I)

# En-têtes de tableau (détection rôles)
TABLE_HEADER_HINTS = {
    "ref":    ("ref", "réf", "reference", "code", "item"),
    "label":  ("désignation", "designation", "libellé", "description", "label", "item description"),
    "qty":    ("qté", "qte", "qty", "quantité", "quantity"),
    "unit":   ("pu", "prix unitaire", "unit price", "price", "unit"),
    "amount": ("montant", "total", "amount", "line total")
}

# =========================
#          Helpers
# =========================
def _norm_amount(s: str) -> Optional[float]:
    if not s: return None
    s = s.strip().replace('\u202f', '').replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None

def _to_int(s: str) -> Optional[int]:
    try:
        if not s: return None
        s2 = re.sub(r"[^\d-]", "", s)
        return int(s2) if s2 not in ("", "-", None) else None
    except Exception:
        return None

def _approx(a: Optional[float], b: Optional[float], tol: float = 1.0) -> bool:
    if a is None or b is None: return False
    return abs(a - b) <= tol

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0
    ht, tva, ttc = total_ht, total_tva, total_ttc
    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc  = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht  is None: ht  = ht_calc
            if tva is None: tva = tva_calc
        except Exception: pass
    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception: pass
    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception: pass
    return ht, tva, ttc

def _norm_header_cell(s: str) -> str:
    s = (s or "").lower().strip()
    s = (s.replace("é","e").replace("è","e").replace("ê","e")
           .replace("à","a").replace("û","u").replace("ï","i"))
    s = s.replace("\n"," ").replace("\t"," ")
    s = re.sub(r"\s+"," ", s)
    return s

def _role_of_header(text: str) -> Optional[str]:
    t = _norm_header_cell(text)
    for role, alts in TABLE_HEADER_HINTS.items():
        if any(a in t for a in alts):
            return role
    return None

# ==== Extraction blocs parties (robuste) ====
def _extract_party_block(text: str, label_patterns: List[str], stop_patterns: List[str]) -> Optional[str]:
    """
    Cherche une étiquette (ex: "Client", "Buyer") et récupère les 3–6 lignes suivantes
    jusqu’à une ligne vide ou un autre en-tête.
    """
    lines = text.splitlines()
    # indices des lignes où apparaît l'étiquette
    label_re = re.compile(r'^\s*(?:' + '|'.join(label_patterns) + r')\s*:?$', re.I)
    stop_re  = re.compile(r'^\s*(?:' + '|'.join(stop_patterns)  + r')\s*:?$', re.I)

    for i, line in enumerate(lines):
        if label_re.match(line.strip()):
            # collecter lignes suivantes
            buf = []
            for j in range(i+1, min(i+12, len(lines))):
                L = lines[j].strip()
                if not L:
                    break
                if stop_re.match(L):
                    break
                buf.append(L)
                if len(buf) >= 6:  # limite raisonnable
                    break
            s = ' '.join(buf).strip()
            return s or None
    return None

# ==== pdfplumber : header + lignes (mots) ====
def _find_header_line(words: List[Dict[str, Any]], y_tol: float = 4.0) -> Optional[Tuple[float, Dict[str, float]]]:
    if not words: return None
    # grouper par Y approx
    rows: Dict[int, List[Dict[str, Any]]] = {}
    band: List[float] = []
    def _closest(y):
        for k, v in enumerate(band):
            if abs(v - y) <= y_tol:
                return k
        band.append(y)
        return len(band)-1

    for w in words:
        k = _closest(w["top"])
        rows.setdefault(k, []).append(w)

    for k in sorted(rows.keys()):
        ws = rows[k]
        roles: Dict[str, float] = {}
        for w in ws:
            role = _role_of_header(w.get("text",""))
            if role and role not in roles:
                roles[role] = (w["x0"] + w["x1"]) / 2.0
        if len(roles) >= 2:
            y_header = sum(w["top"] for w in ws) / len(ws)
            roles = dict(sorted(roles.items(), key=lambda kv: kv[1]))
            return y_header, roles
    return None

def _cluster_rows(words: List[Dict[str, Any]], start_y: float, y_tol: float = 4.0) -> List[List[Dict[str, Any]]]:
    below = [w for w in words if w["top"] > start_y + 0.5]
    below.sort(key=lambda w: (w["top"], w["x0"]))
    lines: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    last = None
    for w in below:
        if last is None or abs(w["top"] - last) <= y_tol:
            cur.append(w)
            last = w["top"] if last is None else (last + w["top"]) / 2.0
        else:
            if cur: lines.append(cur)
            cur = [w]; last = w["top"]
    if cur: lines.append(cur)
    return lines

def _assign_to_columns(line_words: List[Dict[str, Any]], roles_x: Dict[str, float]) -> Dict[str, str]:
    cols: Dict[str, List[str]] = {r: [] for r in roles_x.keys()}
    for w in line_words:
        cx = (w["x0"] + w["x1"]) / 2.0
        role = min(roles_x.keys(), key=lambda r: abs(roles_x[r] - cx))
        cols[role].append(w["text"])
    return {r: " ".join(cols[r]).strip() if cols[r] else "" for r in cols}

def _parse_lines_with_words(page, y_header: float, roles_x: Dict[str, float]) -> List[Dict[str, Any]]:
    words = page.extract_words(extra_attrs=["x0","x1","top","bottom"])
    rows = _cluster_rows(words, start_y=y_header, y_tol=4.0)
    out: List[Dict[str, Any]] = []
    for rw in rows:
        cols = _assign_to_columns(rw, roles_x)
        row_text = " ".join(w["text"] for w in rw).lower()
        if any(k in row_text for k in ("total ht", "total ttc", "tva", "grand total")):
            continue
        ref   = cols.get("ref") or None
        label = cols.get("label") or ref or ""
        qty   = _to_int(cols.get("qty",""))
        pu    = _norm_amount(cols.get("unit",""))
        amt   = _norm_amount(cols.get("amount",""))

        if not label and pu is None and amt is None and qty is None:
            continue

        out.append({
            "ref":        ref,
            "label":      label,
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    # dédoublonnage
    uniq, seen = [], set()
    for r in out:
        key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
        if key in seen: continue
        seen.add(key); uniq.append(r)
    return uniq

# =========================
#     Extraction principale
# =========================
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)

    # 1) Texte brut
    try:
        raw_text = _extract_text(p) or ""
    except Exception:
        raw_text = ""

    # 1.a Masquer les identifiants pour éviter de polluer les montants
    masked = raw_text
    for rx in (TVA_ID_RE, IBAN_RE, SIRET_RE, SIREN_RE):
        masked = rx.sub("[ID]", masked)

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": raw_text.count("\f") + 1 if raw_text else 0,
        "filename": p.name,
    }

    # 2) Champs simples
    m_num = NUM_RE.search(raw_text)
    invoice_number = (m_num.group(1).strip() if m_num else None) or None

    m_date = DATE_RE.search(raw_text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    # Totaux (après masquage)
    total_ht  = _norm_amount((TOTAL_HT_RE.search(masked)  or [None, None])[1]) if TOTAL_HT_RE.search(masked)  else None
    total_tva = _norm_amount((TOTAL_TVA_RE.search(masked) or [None, None])[1]) if TOTAL_TVA_RE.search(masked) else None
    total_ttc = _norm_amount((TOTAL_TTC_RE.search(masked) or [None, None])[1]) if TOTAL_TTC_RE.search(masked) else None

    if total_ttc is None:
        amts = [_norm_amount(a) for a in EUR_RE.findall(masked)]
        amts = [a for a in amts if a is not None]
        if amts:
            total_ttc = max(amts)

    # Devise
    currency = None
    if re.search(r"\bEUR\b|€", raw_text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", raw_text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", raw_text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", raw_text, re.I): currency = "USD"

    # Taux TVA
    vat_rate = None
    m_vr = VAT_RATE_RE.search(raw_text)
    if m_vr:
        vat_rate = m_vr.group(1).replace(',', '.')

    # 3) Parties (robuste à base de lignes adjacentes)
    seller = _extract_party_block(
        raw_text,
        label_patterns=["Émetteur", "Vendeur", "Seller", "From"],
        stop_patterns=["Client", "Acheteur", "Buyer", "Bill(?:ed)?\\s*to", "To", "Facture", "Invoice", "Num(é|e)ro", "Date"]
    )
    buyer = _extract_party_block(
        raw_text,
        label_patterns=["Client", "Acheteur", "Buyer", "Bill(?:ed)?\\s*to", "To"],
        stop_patterns=["Émetteur", "Vendeur", "Seller", "From", "Facture", "Invoice", "Num(é|e)ro", "Date"]
    )

    # IDs FR (non masqués – re-lire sur le texte original)
    seller_tva = None
    seller_siret = None
    seller_iban = None
    m = TVA_ID_RE.search(raw_text)
    if m: seller_tva = m.group(0).replace(' ', '')
    m = SIRET_RE.search(raw_text)
    if m: seller_siret = m.group(0)
    else:
        m2 = SIREN_RE.search(raw_text)
        if m2: seller_siret = m2.group(0)
    m = IBAN_RE.search(raw_text)
    if m: seller_iban = m.group(0).replace(' ', '')

    # 4) Lignes via pdfplumber (premium)
    lines: List[Dict[str, Any]] = []
    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(p)) as pdf:
                for page in pdf.pages:
                    words = page.extract_words(extra_attrs=["x0","x1","top","bottom"])
                    hdr = _find_header_line(words, y_tol=4.0)
                    if not hdr:
                        continue
                    y_header, roles_x = hdr
                    page_lines = _parse_lines_with_words(page, y_header, roles_x)
                    if page_lines:
                        lines.extend(page_lines)
        except Exception:
            pass

    # 5) Fallback lignes (texte)
    if not lines:
        for m in LINE_RX.finditer(raw_text):
            qty = int(m.group('qty'))
            pu  = _norm_amount(m.group('pu'))
            amt = _norm_amount(m.group('amt'))
            lines.append({
                "ref":        m.group('ref'),
                "label":      m.group('label').strip(),
                "qty":        qty,
                "unit_price": pu,
                "amount":     amt
            })

    # 6) Compléter HT/TVA/TTC
    if lines:
        sum_amt = round(sum((r.get("amount") or 0.0) for r in lines), 2) if lines else None
        if sum_amt and total_ttc and _approx(sum_amt, total_ttc, tol=1.5):
            if (total_ht is None) or (total_tva is None):
                ht, tva, ttc = _infer_totals(total_ttc, None, None, vat_rate)
                total_ht  = total_ht  or ht
                total_tva = total_tva or tva
                total_ttc = total_ttc or ttc
        else:
            if sum_amt and total_ht is None:
                total_ht = sum_amt
            if (total_ht is not None) and (total_ttc is None or total_tva is None):
                ht, tva, ttc = _infer_totals(total_ttc, total_ht, total_tva, vat_rate)
                if total_ht is None:  total_ht  = ht
                if total_tva is None: total_tva = tva
                if total_ttc is None: total_ttc = ttc

    # 7) Résultat
    result: Dict[str, Any] = {
        "success": True,
        "meta": {
            "bytes": meta["bytes"], "pages": meta["pages"], "filename": meta["filename"]
        },
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "seller":         seller,
            "seller_siret":   seller_siret,
            "seller_tva":     seller_tva,
            "seller_iban":    seller_iban,
            "buyer":          buyer,
            "total_ht":       total_ht,
            "total_tva":      total_tva,
            "total_ttc":      total_ttc,
            "currency":       currency or "EUR",
            "lines_count":    len(lines) if lines else None,
        },
        "text": raw_text[:20000],
        "text_preview": raw_text[:2000],
    }
    if lines:
        result["lines"] = lines
    return result
