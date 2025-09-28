# app/main.py
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import io, csv, os, shutil, tempfile

from .extractors.pdf_basic import extract_pdf

app = Flask(__name__)
CORS(app)

HTML_FORM = """
<!doctype html><meta charset="utf-8">
<title>BillXpert Converter — Test</title>
<h1>BillXpert Converter — Test</h1>
<form method="post" action="/api/full.csv" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Télécharger CSV COMPLET</button>
</form>
<p style="margin-top:12px">Ou :</p>
<form method="post" action="/api/convert" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Voir JSON brut</button>
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

# ------- JSON brut (tout, avec lines) -------
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

# ------- Résumé JSON (stable) -------
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
            "invoice_date": fields.get("invoice_date"),
            "seller": fields.get("seller"),
            "buyer": fields.get("buyer"),
            "seller_siret": fields.get("seller_siret"),
            "seller_tva": fields.get("seller_tva"),
            "seller_iban": fields.get("seller_iban"),
            "currency": fields.get("currency", "EUR"),
            "total_ht": fields.get("total_ht"),
            "total_tva": fields.get("total_tva"),
            "total_ttc": fields.get("total_ttc"),
            "lines_count": fields.get("lines_count") or len(data.get("lines", []) or []),
        }
        return jsonify(summary)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------- Résumé CSV (1 ligne) -------
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
        fields = data.get("fields", {}) if isinstance(data, dict) else {}

        cols = [
            "invoice_number","invoice_date","seller","buyer",
            "seller_siret","seller_tva","seller_iban",
            "currency","total_ht","total_tva","total_ttc","lines_count"
        ]

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(cols)
        w.writerow([
            fields.get("invoice_number") or "",
            fields.get("invoice_date") or "",
            fields.get("seller") or "",
            fields.get("buyer") or "",
            fields.get("seller_siret") or "",
            fields.get("seller_tva") or "",
            fields.get("seller_iban") or "",
            fields.get("currency") or "EUR",
            fields.get("total_ht") if fields.get("total_ht") is not None else "",
            fields.get("total_tva") if fields.get("total_tva") is not None else "",
            fields.get("total_ttc") if fields.get("total_ttc") is not None else "",
            fields.get("lines_count") or len(data.get("lines", []) or []),
        ])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8")); csv_bytes.seek(0)
        return send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name="billxpert_summary.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------- LIGNES CSV -------
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
        lines = data.get("lines", []) if isinstance(data, dict) else []

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["ref","label","qty","unit_price","amount"])
        for r in (lines or []):
            w.writerow([
                r.get("ref") or "",
                r.get("label") or "",
                r.get("qty") if r.get("qty") is not None else "",
                r.get("unit_price") if r.get("unit_price") is not None else "",
                r.get("amount") if r.get("amount") is not None else ""
            ])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8")); csv_bytes.seek(0)
        return send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name="billxpert_lines.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------- CSV COMPLET (facture + lignes) -------
@app.post("/api/full.csv")
def api_full_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path) or {}
        fields = data.get("fields", {}) if isinstance(data, dict) else {}
        lines  = data.get("lines", []) if isinstance(data, dict) else []

        invoice_cols = [
            "invoice_number","invoice_date","seller","buyer",
            "seller_siret","seller_tva","seller_iban",
            "currency","total_ht","total_tva","total_ttc"
        ]
        line_cols = ["line_ref","line_label","line_qty","line_unit_price","line_amount"]

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(invoice_cols + line_cols)

        if lines:
            for row in lines:
                w.writerow([
                    fields.get("invoice_number") or "",
                    fields.get("invoice_date") or "",
                    fields.get("seller") or "",
                    fields.get("buyer") or "",
                    fields.get("seller_siret") or "",
                    fields.get("seller_tva") or "",
                    fields.get("seller_iban") or "",
                    fields.get("currency") or "EUR",
                    fields.get("total_ht") if fields.get("total_ht") is not None else "",
                    fields.get("total_tva") if fields.get("total_tva") is not None else "",
                    fields.get("total_ttc") if fields.get("total_ttc") is not None else "",
                    row.get("ref") or "",
                    row.get("label") or "",
                    row.get("qty") if row.get("qty") is not None else "",
                    row.get("unit_price") if row.get("unit_price") is not None else "",
                    row.get("amount") if row.get("amount") is not None else "",
                ])
        else:
            w.writerow([
                fields.get("invoice_number") or "",
                fields.get("invoice_date") or "",
                fields.get("seller") or "",
                fields.get("buyer") or "",
                fields.get("seller_siret") or "",
                fields.get("seller_tva") or "",
                fields.get("seller_iban") or "",
                fields.get("currency") or "EUR",
                fields.get("total_ht") if fields.get("total_ht") is not None else "",
                fields.get("total_tva") if fields.get("total_tva") is not None else "",
                fields.get("total_ttc") if fields.get("total_ttc") is not None else "",
                "", "", "", "", ""
            ])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8")); csv_bytes.seek(0)
        return send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name="billxpert_full.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------- (optionnel) ancien endpoint simple pour compatibilité -------
@app.post("/api/convert.csv")
def api_convert_csv_compat():
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
        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8")); csv_bytes.seek(0)
        return send_file(csv_bytes, mimetype="text/csv; charset=utf-8",
                         as_attachment=True, download_name="billxpert_convert.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------- Health -------
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})
