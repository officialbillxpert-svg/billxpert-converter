# app/main.py
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil

# Imports projet
from .extractors.pdf_basic import extract_document  # << auto PDF/Image
from .extractors.summary import summarize_from_text  # inchangé

app = Flask(__name__)
CORS(app)

HTML_FORM = """
<!doctype html><meta charset="utf-8">
<title>BillXpert Converter — Test</title>
<h1>BillXpert Converter — Test</h1>
<p>Testez JSON / Résumé / Lignes à partir d’un PDF ou d’une image (PNG/JPG/TIFF/WebP).</p>
<form method="post" action="/api/summary" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf,image/png,image/jpeg,image/tiff,image/webp" required>
  <button type="submit">Résumé JSON</button>
</form>
"""

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

@app.get("/")
def home():
    return render_template_string(HTML_FORM)

def _save_upload(file_storage):
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    fname = file_storage.filename
    path = os.path.join(tmpdir, fname)
    file_storage.save(path)
    return path, tmpdir

def _allowed(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXT

def _build_summary_from_data(data: dict) -> dict:
    data = data or {}
    if data.get("success") is False:
        # remonter l'erreur telle quelle
        return data

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

    raw_text = data.get("text") if isinstance(data, dict) else None
    if not raw_text:
        try:
            raw_text = " ".join(str(v) for v in fields.values() if v)
        except Exception:
            raw_text = ""

    if raw_text:
        auto = summarize_from_text(raw_text)
        for k, v in (auto or {}).items():
            if summary.get(k) in (None, "", 0) and v not in (None, "", 0):
                summary[k] = v

    return summary

# === JSON BRUT ===
@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not _allowed(f.filename):
        return jsonify({"success": False, "error": "unsupported", "allowed": list(ALLOWED_EXT)}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_document(path)
        return jsonify(data)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Résumé JSON ===
@app.post("/api/summary")
def api_summary():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not _allowed(f.filename):
        return jsonify({"success": False, "error": "unsupported", "allowed": list(ALLOWED_EXT)}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_document(path) or {}
        summary = _build_summary_from_data(data)
        return jsonify(summary)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Résumé CSV (flat) ===
@app.post("/api/summary.csv")
def api_summary_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not _allowed(f.filename):
        return jsonify({"success": False, "error": "unsupported", "allowed": list(ALLOWED_EXT)}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_document(path) or {}
        summary = _build_summary_from_data(data)
        if summary.get("success") is False:
            return jsonify(summary), 500

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        headers = ["invoice_number","invoice_date","seller","seller_siret","seller_tva","seller_iban",
                   "buyer","total_ht","total_tva","total_ttc","currency","lines_count"]
        w.writerow(headers)
        w.writerow([summary.get(h, "") for h in headers])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8")); csv_bytes.seek(0)
        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_summary.csv"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === LIGNES : JSON ===
@app.post("/api/lines")
def api_lines_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not _allowed(f.filename):
        return jsonify({"success": False, "error": "unsupported", "allowed": list(ALLOWED_EXT)}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_document(path) or {}
        if data.get("success") is False:
            return jsonify(data), 500
        lines = data.get("lines") or []
        return jsonify({"success": True, "count": len(lines), "lines": lines})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === LIGNES : CSV ===
@app.post("/api/lines.csv")
def api_lines_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not _allowed(f.filename):
        return jsonify({"success": False, "error": "unsupported", "allowed": list(ALLOWED_EXT)}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_document(path) or {}
        if data.get("success") is False:
            return jsonify(data), 500
        lines = data.get("lines") or []

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["ref","label","qty","unit_price","amount"])
        for r in lines:
            w.writerow([
                r.get("ref",""),
                r.get("label",""),
                r.get("qty",""),
                r.get("unit_price",""),
                r.get("amount",""),
            ])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8")); csv_bytes.seek(0)
        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_lines.csv"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# SANTÉ
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Run local : python -m app.main
    app.run(host="0.0.0.0", port=5000)
