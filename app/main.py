# app/main.py
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil
from typing import Dict

# Imports projet (relatifs au package "app")
from .extractors.pdf_basic import extract_pdf
from .extractors.summary import summarize_from_text  # heuristiques texte

app = Flask(__name__)
CORS(app)

# ------------------------------------------------------------
# Page d’accueil simple pour tester rapidement
HTML_FORM = """
<!doctype html><meta charset="utf-8">
<title>BillXpert Converter — Test</title>
<h1>BillXpert Converter — Test</h1>

<form method="post" action="/api/convert.csv" enctype="multipart/form-data" style="margin-bottom:12px">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Exporter CSV (simple)</button>
</form>

<form method="post" action="/api/convert" enctype="multipart/form-data" style="margin-bottom:12px">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Voir JSON brut</button>
</form>

<form method="post" action="/api/summary" enctype="multipart/form-data" style="margin-bottom:12px">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Résumé JSON</button>
</form>

<form method="post" action="/api/summary.csv" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Résumé CSV</button>
</form>
"""

@app.get("/")
def home():
    return render_template_string(HTML_FORM)

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})
# ------------------------------------------------------------

def _save_upload(file_storage):
    """Sauvegarde le fichier uploadé et renvoie (path, tmpdir)."""
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    path = os.path.join(tmpdir, file_storage.filename)
    file_storage.save(path)
    return path, tmpdir

def _build_summary_from_data(data: dict) -> dict:
    """
    Construit le résumé à partir du dict retourné par extract_pdf(path),
    puis complète les champs manquants via summarize_from_text() si possible.
    """
    data = data or {}
    fields = data.get("fields", {}) if isinstance(data, dict) else {}

    summary: Dict[str, object] = {
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

    # Heuristiques texte pour compléter les trous
    raw_text = data.get("text") if isinstance(data, dict) else None
    if not raw_text:
        try:
            raw_text = " ".join(str(v) for v in fields.values() if v)
        except Exception:
            raw_text = ""

    if raw_text:
        auto = summarize_from_text(raw_text)
        for k, v in auto.items():
            if summary.get(k) in (None, "", 0) and v not in (None, "", 0):
                summary[k] = v

    return summary

# === JSON brut du parseur ===
@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path)  # dict
        return jsonify(data)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === CSV simple (quelques champs) ===
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
        fields = data.get("fields", {}) if isinstance(data, dict) else {}

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["invoice_number", "seller", "buyer", "total", "currency"])
        w.writerow([
            fields.get("invoice_number", ""),
            fields.get("seller", "N/A"),
            fields.get("buyer", "N/A"),
            fields.get("total_ttc", ""),
            fields.get("currency", "EUR"),
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

# === Résumé JSON ===
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
        summary = _build_summary_from_data(data)
        return jsonify(summary)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Résumé CSV ===
@app.post("/api/summary.csv")
def api_summary_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path) or {}
        summary = _build_summary_from_data(data)

        cols = ["invoice_number","invoice_date","seller","seller_siret","seller_tva",
                "seller_iban","buyer","total_ht","total_tva","total_ttc","currency","lines_count"]

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(cols)
        w.writerow([summary.get(c, "") if summary.get(c) is not None else "" for c in cols])

        csv_text = "\ufeff" + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))
        csv_bytes.seek(0)

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_summary.csv"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------------------------------------------------------------
if __name__ == "__main__":
    # Local dev : python -m app.main
    app.run(host="0.0.0.0", port=5000)
