from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil

# Imports projet
from .extractors.pdf_basic import extract_pdf
from .extractors.summary import summarize_from_text, summarize_from_csv

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

# ---------------------------------------------------------------------------

def _save_upload(file_storage):
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    path = os.path.join(tmpdir, file_storage.filename)
    file_storage.save(path)
    return path, tmpdir

# JSON
@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path)             # -> dict (ton parseur)
        return jsonify(data)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# CSV
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
        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["invoice_number", "seller", "buyer", "total", "currency"])

        fields = (data or {}).get("fields", {})
        w.writerow([
            fields.get("invoice_number", ""),
            fields.get("seller", "N/A"),
            fields.get("buyer", "N/A"),
            fields.get("total_ttc", ""),
            fields.get("currency", "EUR"),
        ])

        csv_text = '\ufeff' + out.getvalue()                 # BOM UTF-8
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_convert.csv"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# Résumé
@app.post("/api/summary")
def api_summary():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path)  # dict venant de ton parseur

        # Base du summary
        summary = {
            "invoice_number": None, "invoice_date": None,
            "seller": None, "seller_siret": None, "seller_tva": None, "seller_iban": None,
            "buyer": None,
            "total_ht": None, "total_tva": None, "total_ttc": None,
            "currency": "EUR",
            "lines_count": None,
        }

        fields = (data or {}).get("fields", {}) or {}
        # Champs structurés si déjà fournis par le parseur
        summary["invoice_number"] = fields.get("invoice_number") or summary["invoice_number"]
        summary["invoice_date"]   = fields.get("invoice_date")   or summary["invoice_date"]
        summary["seller"]         = fields.get("seller")         or summary["seller"]
        summary["buyer"]          = fields.get("buyer")          or summary["buyer"]
        summary["total_ht"]       = fields.get("total_ht")       or summary["total_ht"]
        summary["total_tva"]      = fields.get("total_tva")      or summary["total_tva"]
        summary["total_ttc"]      = fields.get("total_ttc")      or summary["total_ttc"]
        summary["currency"]       = fields.get("currency")       or summary["currency"]

        # Heuristiques texte pour compléter
        raw_text = data.get("text") if isinstance(data, dict) else None
        if not raw_text:
            try:
                raw_text = " ".join(str(v) for v in fields.values() if v)
            except Exception:
                raw_text = ""
        if raw_text:
            auto = summarize_from_text(raw_text)
            for k, v in auto.items():
                if v and summary.get(k) in (None, "", 0):
                    summary[k] = v

        # Si tu ajoutes plus tard un export CSV détaillé des lignes :
        # csv_bytes = build_detail_csv(data)  # puis fusion via summarize_from_csv(csv_bytes)

        return jsonify(summary)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run local : python -m app.main
    app.run(host="0.0.0.0", port=5000)
