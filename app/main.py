from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil, re

# Imports projet
from .extractors.pdf_basic import extract_pdf
from .extractors.summary import summarize_from_text  # déjà utilisé dans /api/summary

app = Flask(__name__)
CORS(app)

HTML_FORM = """
<!doctype html><meta charset="utf-8">
<title>BillXpert Converter — Test</title>
<h1>BillXpert Converter — Test</h1>

<form method="post" action="/api/convert.csv" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Exporter CSV (facture)</button>
</form>

<p style="margin-top:12px">Ou test JSON :</p>
<form method="post" action="/api/convert" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Voir JSON</button>
</form>

<p style="margin-top:12px">Export des lignes :</p>
<form method="post" action="/api/lines.csv" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Exporter LIGNES CSV</button>
</form>
"""

@app.get("/")
def home():
    return render_template_string(HTML_FORM)

# ---------------------------------------------------------------------------

def _save_upload(file_storage):
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    path = os.path.join(tmpdir, file_storage.filename)
    file_storage.save(path)
    return path, tmpdir

# -------- helpers pour découper les blocs vendeur / client --------

ZIP_CITY_RE = re.compile(r'\b(\d{4,5})\s+([A-Za-zÀ-ÖØ-öø-ÿ\-\']+)\b')

def _split_party_block(raw: str):
    """
    Essaie de découper 'Ton Entreprise SARL 12 rue X 75001 Paris'
    -> name, address, zip, city
    Heuristique tolérante : 1ère ligne = nom si elle ne commence pas par un numéro.
    """
    if not raw:
        return {"name": None, "address": None, "zip": None, "city": None}

    # normaliser / garder aussi versions multilignes
    s = raw.replace('\r', '').strip()
    lines = [l.strip() for l in re.split(r'\n+', s) if l.strip()]

    # si une seule ligne, on essaie quand même de détecter CP/ville
    joined = ' '.join(lines)
    m_zip = ZIP_CITY_RE.search(joined)
    zip_code, city = (m_zip.group(1), m_zip.group(2)) if m_zip else (None, None)

    # name
    if lines:
        # si la 1ère ligne commence par chiffre -> on considère que tout est adresse,
        # sinon 1ère ligne = nom
        if re.match(r'^\d', lines[0]):
            name = None
            addr_lines = lines
        else:
            name = lines[0]
            addr_lines = lines[1:] if len(lines) > 1 else []

        address = ' '.join(addr_lines).strip() or None
    else:
        name, address = None, None

    # Si zip/city pas trouvés, tente sur la dernière ligne
    if not zip_code or not city:
        if lines:
            m2 = ZIP_CITY_RE.search(lines[-1])
            if m2:
                zip_code, city = m2.group(1), m2.group(2)

    return {
        "name": name,
        "address": address,
        "zip": zip_code,
        "city": city
    }

# ---------------------------------------------------------------------------

# === JSON brut de l’extracteur ===
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
        shutil.rmtree(tmpdir, ignore_errors=True)

# === CSV “facture” enrichi ===
@app.post("/api/convert.csv")
def api_convert_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path) or {}
        fields = (data or {}).get("fields", {}) if isinstance(data, dict) else {}

        seller_raw = fields.get("seller") or ""
        buyer_raw  = fields.get("buyer") or ""

        s = _split_party_block(seller_raw)
        b = _split_party_block(buyer_raw)

        # colonnes à plat
        headers = [
            "invoice_number", "invoice_date", "currency",
            "seller_name", "seller_address", "seller_zip", "seller_city",
            "seller_siret", "seller_tva", "seller_iban",
            "buyer_name", "buyer_address", "buyer_zip", "buyer_city",
            "total_ht", "total_tva", "total_ttc",
            "lines_count"
        ]

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')

        w.writerow(headers)
        w.writerow([
            fields.get("invoice_number") or "",
            fields.get("invoice_date") or "",
            fields.get("currency") or "EUR",

            s["name"] or "",
            s["address"] or "",
            s["zip"] or "",
            s["city"] or "",
            fields.get("seller_siret") or "",
            fields.get("seller_tva") or "",
            fields.get("seller_iban") or "",

            b["name"] or "",
            b["address"] or "",
            b["zip"] or "",
            b["city"] or "",

            fields.get("total_ht") if fields.get("total_ht") is not None else "",
            fields.get("total_tva") if fields.get("total_tva") is not None else "",
            fields.get("total_ttc") if fields.get("total_ttc") is not None else "",
            fields.get("lines_count") if fields.get("lines_count") is not None else "",
        ])

        csv_text = '\ufeff' + out.getvalue()  # BOM UTF-8
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))
        csv_bytes.seek(0)

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_convert.csv"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Export CSV des lignes d’articles ===
@app.post("/api/lines.csv")
def api_lines_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path) or {}
        lines = (data or {}).get("lines") or []

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["ref", "label", "qty", "unit_price", "amount"])

        for r in lines:
            w.writerow([
                r.get("ref", "") or "",
                r.get("label", "") or "",
                r.get("qty", "") if r.get("qty") is not None else "",
                r.get("unit_price", "") if r.get("unit_price") is not None else "",
                r.get("amount", "") if r.get("amount") is not None else "",
            ])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))
        csv_bytes.seek(0)

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_lines.csv"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Résumé JSON (champs principaux) ===
@app.post("/api/summary")
def api_summary():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path) or {}
        fields = data.get("fields", {}) if isinstance(data, dict) else {}

        summary = {
            "invoice_number": fields.get("invoice_number"),
            "invoice_date":   fields.get("invoice_date"),
            "seller":         fields.get("seller"),
            "seller_siret":   fields.get("seller_siret"),
            "seller_tva":     fields.get("seller_tva"),
            "seller_iban":    fields.get("seller_iban"),
            "buyer":          fields.get("buyer"),
            "total_ht":       fields.get("total_ht"),
            "total_tva":      fields.get("total_tva"),
            "total_ttc":      fields.get("total_ttc"),
            "currency":       fields.get("currency", "EUR"),
            "lines_count":    fields.get("lines_count"),
        }

        # Compléter via texte brut si dispo
        raw_text = data.get("text") if isinstance(data, dict) else None
        if raw_text:
            auto = summarize_from_text(raw_text)
            for k, v in auto.items():
                if summary.get(k) in (None, "", 0) and v not in (None, "", 0):
                    summary[k] = v

        return jsonify(summary)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})
