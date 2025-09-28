# app/extractors/pdf_basic.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import re

# --- deps d'extraction ---
from pdfminer.high_level import extract_text as pdfminer_extract_text

# pdfplumber est optionnel mais recommandé
try:
    import pdfplumber
except Exception:
    pdfplumber = None

from dateutil import parser as dateparser


# =========================
#        REGEX de base
# =========================
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

# Totaux – on matche plusieurs formulations, FR & EN
TOTAL_TTC_RE = re.compile(r'(?:Total\s*(?:TTC)?|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*:?\s*([0-9][0-9\.\,\s]+)', re.I)
TOTAL_HT_RE  = re.compile(r'(?:Total\s*HT|Sous-?total|Sub\s*total)\s*:?\s*([0-9][0-9\.\,\s]+)', re.I)
TOTAL_TVA_RE = re.compile(r'(?:TVA|VAT)\s*\(?\s*\d+(?:[.,]\d+)?%\s*\)?\s*:?\s*([0-9][0-9\.\,\s]+)', re.I)

EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

# Identifiants FR
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# Blocs parties
SELLER_BLOCK = re.compile(r'(?:Émetteur|Vendeur|Seller|From)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer|Bill(?:ed)?\s*to|To)', re.I | re.S)
CLIENT_BLOCK = re.compile(r'(?:Client|Acheteur|Buyer|Bill(?:ed)?\s*to|To)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller|From)', re.I | re.S)

# Lignes (fallback texte – quand pas d’en-tête trouvée)
# Ex: "PREST-001 — Libellé ...  12  10,00 €  120,00 €"
LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

# Taux TVA
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%?', re.I)


# =========================
#         Helpers
# =========================
def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace('\u202f', '').replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except Exception:
        return None

def _clean_block(s: str) -> Optional[str]:
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s or None

def _approx(a: Optional[float], b: Optional[float], tol: float = 1.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol

def _infer_totals(total_ttc, total_ht, total_tva, vat_rate) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Complète HT/TVA/TTC si on connaît un taux (ex: 20 -> 0.20)."""
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0

    ht, tva, ttc = total_ht, total_tva, total_ttc

    # Cas 1 : seulement TTC connu
    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc  = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht  is None: ht  = ht_calc
            if tva is None: tva = tva_calc
        except Exception:
            pass

    # Cas 2 : HT connu
    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception:
            pass

    # Cas 3 : TTC + TVA connus -> calc HT
    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc


# =========================
#   pdfplumber – tables
# =========================
TABLE_HEADER_HINTS = {
    "ref":    ("ref", "réf", "reference", "code", "item"),
    "label":  ("désignation", "designation", "libellé", "description", "label", "item description"),
    "qty":    ("qté", "qte", "qty", "quantité", "quantity"),
    "unit":   ("pu", "prix unitaire", "unit price", "price", "unit"),
    "amount": ("montant", "total", "amount", "line total")
}

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

def _find_header_line(words: List[Dict[str, Any]], y_tol: float = 4.0) -> Optional[Tuple[float, Dict[str, float]]]:
    """
    Trouve la ligne d’en-tête en groupant les mots par Y, puis en
    détectant des libellés connus. Retourne (y_header, {role: x_center})
    """
    if not words:
        return None
    # groupe par Y (lignes)
    rows_map: Dict[int, List[Dict[str, Any]]] = {}
    ys_sorted = sorted(set(round(w["top"], 1) for w in words))
    def _closest_row(y):
        for i, ry in enumerate(ys_sorted):
            if abs(ry - y) <= y_tol:
                return i
        ys_sorted.append(round(y,1))
        return len(ys_sorted)-1

    for w in words:
        i = _closest_row(w["top"])
        rows_map.setdefault(i, []).append(w)

    # pour chaque "ligne", tenter de détecter des headers
    best = None
    for i, ws in rows_map.items():
        roles = {}
        for w in ws:
            role = _role_of_header(w.get("text",""))
            if role and role not in roles:
                # centre X de ce header
                x_center = (w["x0"] + w["x1"]) / 2.0
                roles[role] = x_center
        # on considère une en-tête valide si >= 2 rôles trouvés
        if len(roles) >= 2:
            y_vals = [w["top"] for w in ws]
            y_header = sum(y_vals)/len(y_vals)
            # ordonner roles par x croissant
            roles = dict(sorted(roles.items(), key=lambda kv: kv[1]))
            best = (y_header, roles)
            break
    return best

def _cluster_rows(words: List[Dict[str, Any]], start_y: float, y_tol: float = 4.0) -> List[List[Dict[str, Any]]]:
    """
    Prend les mots situés sous start_y et groupe par lignes (Y proches).
    """
    below = [w for w in words if w["top"] > start_y + 0.5]
    below.sort(key=lambda w: (w["top"], w["x0"]))
    lines: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    last_top = None

    for w in below:
        if last_top is None or abs(w["top"] - last_top) <= y_tol:
            current.append(w)
            last_top = w["top"] if last_top is None else (last_top + w["top"]) / 2.0
        else:
            if current:
                lines.append(current)
            current = [w]
            last_top = w["top"]

    if current:
        lines.append(current)
    return lines

def _assign_to_columns(line_words: List[Dict[str, Any]], roles_x: Dict[str, float]) -> Dict[str, str]:
    """
    Pour une ligne (liste de mots), assigne chaque mot à la colonne dont le X est le plus proche.
    Concatène les mots par colonne.
    """
    cols: Dict[str, List[str]] = {r: [] for r in roles_x.keys()}
    for w in line_words:
        x_center = (w["x0"] + w["x1"]) / 2.0
        # colonne la plus proche
        role = min(roles_x.keys(), key=lambda r: abs(roles_x[r] - x_center))
        cols[role].append(w["text"])
    # joindre
    return {r: " ".join(cols[r]).strip() if cols[r] else "" for r in cols}

def _to_int(s: str) -> Optional[int]:
    try:
        if not s: return None
        s2 = re.sub(r"[^\d-]", "", s)
        return int(s2) if s2 not in ("", "-", None) else None
    except Exception:
        return None

def _parse_lines_with_words(page, y_header: float, roles_x: Dict[str, float]) -> List[Dict[str, Any]]:
    """Construit les lignes d’articles à partir des mots et des colonnes détectées."""
    all_words = page.extract_words(extra_attrs=["x0","x1","top","bottom"])
    rows = _cluster_rows(all_words, start_y=y_header, y_tol=4.0)

    out: List[Dict[str, Any]] = []
    for rw in rows:
        cols = _assign_to_columns(rw, roles_x)
        ref   = cols.get("ref") or None
        label = cols.get("label") or ref or ""
        qty   = _to_int(cols.get("qty",""))
        pu    = _norm_amount(cols.get("unit",""))
        amt   = _norm_amount(cols.get("amount",""))

        # ignorer les balises "Total", "TVA", etc.
        row_text = " ".join(w["text"] for w in rw).lower()
        if any(k in row_text for k in ("total ht", "total ttc", "tva", "grand total")):
            continue

        # ignorer lignes vides
        if not label and pu is None and amt is None and qty is None:
            continue

        out.append({
            "ref":        ref,
            "label":      label,
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    # nettoyage doublons
    uniq, seen = [], set()
    for r in out:
        key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


# =========================
#  Extraction principale
# =========================
def extract_pdf(path: str) -> Dict[str, Any]:
    p = Path(path)

    # 1) Texte brut (utile pour regex + blocs parties)
    text = ""
    try:
        text = pdfminer_extract_text(p) or ""
    except Exception:
        text = ""

    meta = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": text.count("\f") + 1 if text else 0,
        "filename": p.name,
    }

    # --- Champs simples ---
    m_num = NUM_RE.search(text)
    invoice_number = (m_num.group(1).strip() if m_num else None) or None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    # Totaux par libellés (si présents tels quels)
    total_ht  = _norm_amount((TOTAL_HT_RE.search(text)  or [None, None])[1]) if TOTAL_HT_RE.search(text)  else None
    total_tva = _norm_amount((TOTAL_TVA_RE.search(text) or [None, None])[1]) if TOTAL_TVA_RE.search(text) else None
    total_ttc = _norm_amount((TOTAL_TTC_RE.search(text) or [None, None])[1]) if TOTAL_TTC_RE.search(text) else None

    # fallback TTC si rien trouvé: prendre la plus grande somme plausible
    if total_ttc is None:
        amts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amts = [a for a in amts if a is not None]
        if amts:
            total_ttc = max(amts)

    # Devise
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"

    # Taux TVA (pour compléter si nécessaire)
    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vat_rate = m_vat.group(1).replace(',', '.')

    # Parties
    seller = None
    buyer  = None
    m = SELLER_BLOCK.search(text)
    if m: seller = _clean_block(m.group('blk'))
    m = CLIENT_BLOCK.search(text)
    if m: buyer = _clean_block(m.group('blk'))

    # IDs FR
    seller_tva = None
    seller_siret = None
    seller_iban = None

    m = TVA_RE.search(text)
    if m: seller_tva = m.group(0).replace(' ', '')
    m = SIRET_RE.search(text)
    if m: seller_siret = m.group(0)
    else:
        m2 = SIREN_RE.search(text)
        if m2: seller_siret = m2.group(0)
    m = IBAN_RE.search(text)
    if m: seller_iban = m.group(0).replace(' ', '')

    # 2) Lignes d’articles via pdfplumber (premium)
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
            # on reste silencieux et on bascule fallback texte
            pass

    # 3) Fallback lignes via regex (si rien trouvé)
    if not lines:
        for m in LINE_RX.finditer(text):
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

    # 4) Compléter HT/TVA/TTC si possible à partir des lignes ou du taux
    if lines:
        # Si les montants de lignes existent, on somme
        sum_amounts = sum([(r.get("amount") or 0.0) for r in lines])
        sum_amounts = round(sum_amounts, 2) if sum_amounts else None

        if sum_amounts and total_ttc and _approx(sum_amounts, total_ttc, tol=1.5):
            # lignes ≈ TTC
            if total_ht is None or total_tva is None:
                ht, tva, ttc = _infer_totals(total_ttc, None, None, vat_rate)
                total_ht  = total_ht  or ht
                total_tva = total_tva or tva
                total_ttc = total_ttc or ttc
        else:
            # considérer que lignes = HT (cas fréquent)
            if sum_amounts and total_ht is None:
                total_ht = sum_amounts
            if (total_ht is not None) and (total_ttc is None or total_tva is None):
                ht, tva, ttc = _infer_totals(total_ttc, total_ht, total_tva, vat_rate)
                total_ht  = ht  if total_ht is None else total_ht
                total_tva = tva if total_tva is None else total_tva
                total_ttc = ttc if total_ttc is None else total_ttc

    # 5) Résultat
    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number or None,
            "invoice_date":   invoice_date,
            "total_ht":  total_ht,
            "total_tva": total_tva,
            "total_ttc": total_ttc,
            "currency":  currency or "EUR",
            "seller":    seller,
            "buyer":     buyer,
            "seller_tva":   seller_tva,
            "seller_siret": seller_siret,
            "seller_iban":  seller_iban,
            "lines_count": len(lines) if lines else None,
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    if lines:
        result["lines"] = lines

    return result
