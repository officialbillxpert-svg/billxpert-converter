from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv
# après
from .extractors.pdf_basic import extract_pdf
from extractors.summary import summarize_from_text, summarize_from_csv

app = Flask(__name__)
CORS(app)

HTML_FORM = """
<!doctype html><meta charset="utf-8">
<title>BillXpert Converter — Test</title>
<h1>BillXpert Converter — Test</h1>
<form method="post" action="/api/convert.csv" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Exporter CSV</button>
</form>
<p style="margin-top:12px">Ou test JSON :</p>
<form method="post" action="/api/convert" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Voir JSON</button>
</form>
"""

@app.get("/")
def home():
    return render_template_string(HTML_FORM)

def _save_upload(file_storage):
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    path = os.path.join(tmpdir, file_storage.filename)
    file_storage.save(path)
    return path, tmpdir

@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path)
        return jsonify(data)
    finally:
        try:
            import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
        except:
            pass

@app.post("/api/convert.csv")
def api_convert_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path)

        # CSV en ; + CRLF + BOM pour Excel/Calc
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
        writer.writerow(["invoice_number","seller","buyer","total","currency"])

        fields = data.get("fields", {})
        writer.writerow([
            fields.get("invoice_number",""),
            fields.get("seller","N/A"),
            fields.get("buyer","N/A"),
            fields.get("total_ttc",""),
            fields.get("currency","EUR"),
        ])

        csv_text = '\ufeff' + output.getvalue()          # BOM UTF-8
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_convert.csv"
        )
    finally:
        try:
            import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
        except:
            pass

# --- helpers extraction facture (France) ---
import re, csv, io, datetime as dt
from typing import Dict, Any, List, Optional

AMOUNT_RE = re.compile(r'(?<!\d)(?:\d{1,3}(?:[ .]\d{3})*|\d+)(?:[,.]\d{2})?(?!\d)')
DATE_RES = [
    re.compile(r'(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})'),
    re.compile(r'(\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2})'),
]
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

def _norm_amount(s: str) -> Optional[float]:
    if not s: return None
    s = s.strip().replace(' ', '').replace('\u202f','')
    s = s.replace('.', '').replace(',', '.') if s.count(',') >= 1 and s.count('.') <= 1 else s
    try: return float(s)
    except: return None

def _parse_date(s: str) -> Optional[str]:
    s = s.replace(' ', '')
    for rx in DATE_RES:
        m = rx.search(s)
        if not m: continue
        d = m.group(1).replace('.', '/').replace('-', '/')
        parts = d.split('/')
        try:
            if len(parts[0]) == 4:  # YYYY/MM/DD
                t = dt.datetime.strptime(d, '%Y/%m/%d')
            else:                   # DD/MM/YYYY ou DD/MM/YY
                fmt = '%d/%m/%Y' if len(parts[-1]) == 4 else '%d/%m/%y'
                t = dt.datetime.strptime(d, fmt)
            return t.date().isoformat()
        except: 
            continue
    return None

def summarize_from_text(text: str) -> Dict[str, Any]:
    # Cherche entêtes & totaux probables
    out = {
        "invoice_number": None, "invoice_date": None,
        "seller": None, "seller_siret": None, "seller_tva": None, "seller_iban": None,
        "buyer": None,
        "total_ht": None, "total_tva": None, "total_ttc": None,
        "currency": "EUR",
        "lines_count": None,
    }
    # heuristiques simples
    # n° facture
    m = re.search(r'(?:facture|invoice)\s*(?:n[°o]\s*|#\s*)?([A-Z0-9\-\/\.]{3,})', text, re.I)
    if m: out["invoice_number"] = m.group(1).strip()

    # date
    out["invoice_date"] = _parse_date(text)

    # SIRET/SIREN/TVA
    m = TVA_RE.search(text);   out["seller_tva"]   = m.group(0).replace(' ', '') if m else None
    m = SIRET_RE.search(text); out["seller_siret"] = m.group(0) if m else (SIREN_RE.search(text).group(0) if SIREN_RE.search(text) else None)

    # IBAN
    m = IBAN_RE.search(text);  out["seller_iban"]  = m.group(0).replace(' ', '') if m else None

    # Totaux (on essaie des libellés fr/us)
    def grab_total(label_rx):
        m = re.search(label_rx + r'.{0,30}?' + AMOUNT_RE.pattern, text, re.I)
        if not m: return None
        last = AMOUNT_RE.findall(m.group(0))
        return _norm_amount(last[-1]) if last else None

    out["total_ttc"] = grab_total(r'(total\s*(ttc|€)|montant\s+ttc|total\s+amount|grand\s+total)')
    out["total_ht"]  = grab_total(r'(total\s*ht|montant\s+ht|subtotal|sous-total)')
    out["total_tva"] = grab_total(r'(tva|vat\s*total|tax\s*total)')

    return out

def summarize_from_csv(csv_bytes: bytes) -> Dict[str, Any]:
    f = io.StringIO(csv_bytes.decode('utf-8-sig'))
    rd = csv.DictReader(f, delimiter=';')
    total_ht = total_tva = total_ttc = 0.0
    n_lines = 0
    for row in rd:
        n_lines += 1
        ht  = _norm_amount(row.get('total_ht') or row.get('HT') or row.get('total') or '')
        tva = _norm_amount(row.get('tva') or row.get('TVA') or '')
        ttc = _norm_amount(row.get('total_ttc') or row.get('TTC') or '')
        if ht  is not None: total_ht  += ht
        if tva is not None: total_tva += tva
        if ttc is not None: total_ttc += ttc
    return {
        "invoice_number": None, "invoice_date": None,
        "seller": None, "seller_siret": None, "seller_tva": None, "seller_iban": None,
        "buyer": None,
        "total_ht": round(total_ht,2) if total_ht else None,
        "total_tva": round(total_tva,2) if total_tva else None,
        "total_ttc": round(total_ttc,2) if total_ttc else None,
        "currency": "EUR",
        "lines_count": n_lines or None,
    }

# --- nouvelle route ---
from fastapi import UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

@app.post("/api/summary")
async def summary(file: UploadFile = File(...)):
    # 1) Valide PDF
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, detail="PDF uniquement")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 8*1024*1024:
        raise HTTPException(413, detail="Fichier trop volumineux")

    # 2) Passe par TON parseur existant
    # Remplace ces 2 appels par tes vraies fonctions:
    #   parse_to_json(pdf_bytes) -> dict
    #   parse_to_csv(pdf_bytes)  -> bytes (UTF-8-SIG, ';')
    json_data: Dict[str, Any] = parse_to_json(pdf_bytes)  # TODO: ta fonction
    csv_data:  Optional[bytes] = None
    try:
        csv_data = parse_to_csv(pdf_bytes)                # TODO: ta fonction
    except Exception:
        csv_data = None

    # 3) Résumé
    summary: Dict[str, Any]
    if csv_data:
        summary = summarize_from_csv(csv_data)
    else:
        # cherche un champ 'text' ou concatène le JSON pour heuristiques
        raw_text = json_data.get('text') if isinstance(json_data, dict) else None
        if not raw_text:
            raw_text = '\n'.join(map(str, json_data)) if isinstance(json_data, (list, tuple)) else str(json_data)
        summary = summarize_from_text(raw_text)

    # 4) essaie de remplir quelques trous depuis le JSON structuré si existant
    if isinstance(json_data, dict):
        summary["invoice_number"] = summary["invoice_number"] or json_data.get("invoice_number")
        summary["invoice_date"]   = summary["invoice_date"]   or json_data.get("invoice_date")
        party = json_data.get("seller") or {}
        summary["seller"]       = summary["seller"]       or party.get("name")
        summary["seller_siret"] = summary["seller_siret"] or party.get("siret")
        summary["seller_tva"]   = summary["seller_tva"]   or party.get("vat")
        summary["seller_iban"]  = summary["seller_iban"]  or party.get("iban")
        buyer = json_data.get("buyer") or {}
        summary["buyer"]        = summary["buyer"]        or buyer.get("name")

        # totaux (si CSV absent)
        totals = json_data.get("totals") or {}
        summary["total_ht"]  = summary["total_ht"]  or totals.get("ht")
        summary["total_tva"] = summary["total_tva"] or totals.get("tva")
        summary["total_ttc"] = summary["total_ttc"] or totals.get("ttc")
        lines = json_data.get("lines")
        if lines and not summary.get("lines_count"):
            summary["lines_count"] = len(lines)

    return JSONResponse(summary)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
