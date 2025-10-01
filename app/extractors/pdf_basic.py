# app/extractors/pdf_basic.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import re

# ---- Imports "safe" (tout protégé pour éviter les crashs au démarrage) ----
try:
    from pdfminer.high_level import extract_text as _extract_text
except Exception:
    _extract_text = None

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    import pikepdf  # utile pour certains PDF problématiques (optionnel)
except Exception:
    pikepdf = None

# OCR (facultatif) : entièrement protégé
try:
    from PIL import Image
except Exception:
    Image = None
try:
    import pytesseract
except Exception:
    pytesseract = None

from dateutil import parser as dateparser


# ----------------------- REGEX & CONSTANTES -----------------------
NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')
TOTAL_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Montant\s*TTC|Total\s*à\s*payer|Grand\s*total|Total\s*amount)\s*[:€]*\s*([0-9][0-9\.\,\s]+)',
    re.I
)
EUR_RE   = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2})?)')

SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)

LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]


# ----------------------- HELPERS GÉNÉRIQUES -----------------------
def _norm_amount(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(' ', '')
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
    if vat_rate is None:
        return total_ht, total_tva, total_ttc
    rate = float(str(vat_rate).replace(',', '.')) / 100.0
    ht, tva, ttc = total_ht, total_tva, total_ttc

    if ttc is not None and (ht is None or tva is None):
        try:
            ht_calc = round(ttc / (1.0 + rate), 2)
            tva_calc = round(ttc - ht_calc, 2)
            if ht is None:  ht = ht_calc
            if tva is None: tva = tva_calc
        except Exception:
            pass

    if ht is not None and (ttc is None or tva is None):
        try:
            tva_calc = round(ht * rate, 2)
            ttc_calc = round(ht + tva_calc, 2)
            if tva is None: tva = tva_calc
            if ttc is None: ttc = ttc_calc
        except Exception:
            pass

    if ttc is not None and tva is not None and ht is None:
        try:
            ht = round(ttc - tva, 2)
        except Exception:
            pass

    return ht, tva, ttc


# ----------------------- OCR -----------------------
def _ocr_image_to_text(path: Path) -> Dict[str, Any]:
    """
    OCR d'une image (PNG/JPG). Retourne { success, text, error? }.
    Si pytesseract/PIL/tesseract binaire manquent -> success=False + error.
    """
    if Image is None or pytesseract is None:
        return {"success": False, "error": "tesseract_not_found", "details": "PIL/pytesseract non installés."}
    try:
        img = Image.open(path)
    except Exception as e:
        return {"success": False, "error": "image_open_failed", "details": str(e)}

    try:
        txt = pytesseract.image_to_string(img, lang="fra+eng")
        return {"success": True, "text": txt or ""}
    except pytesseract.pytesseract.TesseractNotFoundError:
        return {"success": False, "error": "tesseract_not_found", "details": "Binaire tesseract absent."}
    except Exception as e:
        return {"success": False, "error": "ocr_failed", "details": str(e)}


# ----------------------- EXTRACTION DES LIGNES -----------------------
def _norm_header_cell(s: str) -> str:
    s = (s or "").strip().lower()
    s = (s.replace("é","e").replace("è","e").replace("ê","e")
           .replace("à","a").replace("û","u").replace("ï","i"))
    s = s.replace("\n"," ").replace("\t"," ")
    s = re.sub(r"\s+"," ", s)
    return s

def _map_header_indices(headers: List[str]) -> Optional[Dict[str, int]]:
    idx: Dict[str, Optional[int]] = {}
    norm = [_norm_header_cell(h) for h in headers]

    def match_one(*cands):
        for i, h in enumerate(norm):
            for c in cands:
                if c in h:
                    return i
        return None

    idx["ref"]    = match_one(*TABLE_HEADER_HINTS[0])
    idx["label"]  = match_one(*TABLE_HEADER_HINTS[1])
    idx["qty"]    = match_one(*TABLE_HEADER_HINTS[2])
    idx["unit"]   = match_one(*TABLE_HEADER_HINTS[3])
    idx["amount"] = match_one(*TABLE_HEADER_HINTS[4])

    if all(v is None for v in idx.values()):
        return None
    return {k: v for k, v in idx.items() if v is not None}

def _parse_lines_with_pdfplumber(pdf_path: str) -> List[Dict[str, Any]]:
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

                for tbl in tables:
                    tbl = [[(c or "").strip() for c in (row or [])] for row in (tbl or []) if any((row or []))]
                    if not tbl or len(tbl) < 2:
                        continue
                    header = tbl[0]
                    idx = _map_header_indices(header)
                    if not idx:
                        continue

                    for line in tbl[1:]:
                        def get(i): return line[i] if (i is not None and i < len(line)) else ""
                        ref   = get(idx.get("ref"))
                        label = get(idx.get("label")) or ref
                        qty   = get(idx.get("qty"))
                        pu    = get(idx.get("unit"))
                        amt   = get(idx.get("amount"))

                        try:
                            qty = int(re.sub(r"[^\d]", "", qty)) if qty else None
                        except Exception:
                            qty = None

                        pu_f  = _norm_amount(pu)
                        amt_f = _norm_amount(amt)

                        if not (label or pu_f is not None or amt_f is not None):
                            continue

                        rows.append({
                            "ref":        (ref or "").strip() or None,
                            "label":      (label or "").strip(),
                            "qty":        qty,
                            "unit_price": pu_f,
                            "amount":     amt_f
                        })
        # dédoublonnage
        uniq, seen = [], set()
        for r in rows:
            key = (r.get("ref"), r.get("label"), r.get("qty"), r.get("unit_price"), r.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        return uniq
    except Exception:
        return []

def _parse_lines(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in LINE_RX.finditer(text):
        qty = int(m.group('qty'))
        pu  = _norm_amount(m.group('pu'))
        amt = _norm_amount(m.group('amt'))
        rows.append({
            "ref":        m.group('ref'),
            "label":      m.group('label').strip(),
            "qty":        qty,
            "unit_price": pu,
            "amount":     amt
        })
    return rows


# ----------------------- EXTRACTION PRINCIPALE -----------------------
def _extract_text_from_pdf(path: Path) -> str:
    if _extract_text is None:
        return ""
    try:
        return _extract_text(str(path)) or ""
    except Exception:
        # pikepdf peut parfois "réparer" un PDF puis on retente
        if pikepdf is not None:
            try:
                fixed = path.with_suffix(".repaired.pdf")
                with pikepdf.open(str(path)) as pdf:
                    pdf.save(str(fixed))
                return _extract_text(str(fixed)) or ""
            except Exception:
                return ""
        return ""

def extract_document(path: str, ocr: Optional[str] = None) -> Dict[str, Any]:
    """
    ocr: "auto" (defaut), "force", "off"
    - PDF texte → pdfminer
    - Image (png/jpg) → OCR si dispo/autorisé
    - PDF scanné → si auto & texte vide, on renvoie succès False avec erreur explicite d'OCR manquant
    """
    p = Path(path)
    suffix = p.suffix.lower()
    is_pdf  = suffix == ".pdf"
    is_img  = suffix in {".png", ".jpg", ".jpeg"}

    ocr_mode = (ocr or "auto").lower()
    use_ocr = False
    text = ""

    meta: Dict[str, Any] = {
        "bytes": p.stat().st_size if p.exists() else None,
        "pages": 0,
        "filename": p.name,
    }

    # --- Route image ---
    if is_img:
        meta["from_images"] = True
        meta["ocr_used"] = True
        ocr_res = _ocr_image_to_text(p)
        if not ocr_res.get("success"):
            return {
                "success": False,
                "error": ocr_res.get("error") or "ocr_failed",
                "details": ocr_res.get("details"),
                "meta": meta,
                "fields": {
                    "invoice_number": None, "invoice_date": None,
                    "total_ht": None, "total_tva": None, "total_ttc": None,
                    "currency": "EUR",
                },
                "text": "", "text_preview": ""
            }
        text = ocr_res.get("text", "") or ""
        meta["ocr_pages"] = 1
        # on n'a pas d'info "pages" pour image unique
        meta["pages"] = 0

    # --- Route PDF ---
    elif is_pdf:
        text = _extract_text_from_pdf(p)
        meta["pages"] = (text.count("\f") + 1) if text else 0
        if (ocr_mode in ("force",)) or (ocr_mode in ("auto", None) and not text.strip()):
            # PDF scanné → proposer OCR (mais on ne rasterize pas pages ici pour rester léger)
            # On signale clairement l’absence d’OCR si tesseract/PIL manquent
            if pytesseract is None or Image is None:
                return {
                    "success": False,
                    "error": "tesseract_not_found",
                    "details": "OCR requis (PDF scanné) mais Tesseract/PIL indisponibles.",
                    "meta": {**meta, "ocr_used": True},
                    "fields": {
                        "invoice_number": None, "invoice_date": None,
                        "total_ht": None, "total_tva": None, "total_ttc": None,
                        "currency": "EUR",
                    },
                    "text": "", "text_preview": ""
                }
            # Si on veut aller plus loin, il faudrait rasterizer les pages en images puis OCR.
            # Ici on reste explicite : OCR non implémenté pour PDF multipage scanné.
            return {
                "success": False,
                "error": "pdf_scanned_no_raster",
                "details": "Le PDF semble scanné. Implémente la rasterization + OCR pour continuer.",
                "meta": {**meta, "ocr_used": True},
                "fields": {
                    "invoice_number": None, "invoice_date": None,
                    "total_ht": None, "total_tva": None, "total_ttc": None,
                    "currency": "EUR",
                },
                "text": "", "text_preview": ""
            }
    else:
        return {
            "success": False,
            "error": "unsupported_file",
            "details": f"Extension non supportée: {suffix}",
            "meta": meta,
            "fields": {
                "invoice_number": None, "invoice_date": None,
                "total_ht": None, "total_tva": None, "total_ttc": None,
                "currency": "EUR",
            },
            "text": "", "text_preview": ""
        }

    # ---------------- Champs simples ----------------
    currency = None
    if re.search(r"\bEUR\b|€", text, re.I): currency = "EUR"
    elif re.search(r"\bGBP\b|£", text, re.I): currency = "GBP"
    elif re.search(r"\bCHF\b", text, re.I): currency = "CHF"
    elif re.search(r"\bUSD\b|\$", text, re.I): currency = "USD"

    m_num = NUM_RE.search(text)
    invoice_number = m_num.group(1).strip() if m_num else None

    m_date = DATE_RE.search(text)
    invoice_date = None
    if m_date:
        try:
            invoice_date = dateparser.parse(m_date.group(1), dayfirst=True).date().isoformat()
        except Exception:
            invoice_date = None

    m_total = TOTAL_RE.search(text)
    total_ttc = _norm_amount(m_total.group(1)) if m_total else None
    if total_ttc is None:
        amts = [_norm_amount(a) for a in EUR_RE.findall(text)]
        amts = [a for a in amts if a is not None]
        if amts:
            total_ttc = max(amts)

    vat_rate = None
    m_vat = VAT_RATE_RE.search(text)
    if m_vat:
        vr = m_vat.group(1)
        vat_rate = '5.5' if vr in ('5,5', '5.5') else vr

    result: Dict[str, Any] = {
        "success": True,
        "meta": meta,
        "fields": {
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "total_ht":  None,
            "total_tva": None,
            "total_ttc": total_ttc,
            "currency":  currency or "EUR",
        },
        "text": text[:20000],
        "text_preview": text[:2000],
    }
    fields = result["fields"]

    # Vendeur / Client
    m = SELLER_BLOCK.search(text)
    if m and not fields.get("seller"):
        fields["seller"] = _clean_block(m.group('blk'))
    m = CLIENT_BLOCK.search(text)
    if m and not fields.get("buyer"):
        fields["buyer"] = _clean_block(m.group('blk'))

    # Identifiants
    m = TVA_RE.search(text)
    if m and not fields.get("seller_tva"):
        fields["seller_tva"] = m.group(0).replace(' ', '')
    m = SIRET_RE.search(text)
    if m and not fields.get("seller_siret"):
        fields["seller_siret"] = m.group(0)
    elif not fields.get("seller_siret"):
        m2 = SIREN_RE.search(text)
        if m2:
            fields["seller_siret"] = m2.group(0)
    m = IBAN_RE.search(text)
    if m and not fields.get("seller_iban"):
        fields["seller_iban"] = m.group(0).replace(' ', '')

    # ---------------- LIGNES ----------------
    lines: List[Dict[str, Any]] = []
    # pdfplumber d’abord (PDF uniquement)
    if is_pdf:
        try:
            lines = _parse_lines_with_pdfplumber(str(p))
        except Exception:
            lines = []
    # fallback regex
    if not lines and text:
        lines = _parse_lines(text)

    if lines:
        result["lines"] = lines
        fields["lines_count"] = len(lines)

        sum_lines = round(sum((r.get("amount") or 0.0) for r in lines), 2)
        if total_ttc and sum_lines and _approx(sum_lines, total_ttc, tol=1.5):
            total_ht, total_tva, total_ttc2 = _infer_totals(total_ttc, None, None, vat_rate)
            fields["total_ht"]  = total_ht
            fields["total_tva"] = total_tva
            fields["total_ttc"] = total_ttc2 or total_ttc
        else:
            total_ht = sum_lines if sum_lines else fields.get("total_ht")
            th, tv, tt = _infer_totals(total_ttc, total_ht, fields.get("total_tva"), vat_rate)
            if th is not None: fields["total_ht"]  = th
            if tv is not None: fields["total_tva"] = tv
            if tt is not None: fields["total_ttc"] = tt

    return result


# ---- Alias rétro-compatibilité : ne casse pas les imports existants ----
def extract_pdf(path: str, **kwargs):
    return extract_document(path, **kwargs)
