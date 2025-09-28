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

# --- JSON brut (tout, avec lines) ---
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

# --- CSV COMPLET : en-têtes facture + lignes (une ligne par article) ---
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

        # colonnes “facture”
        invoice_cols = [
            "invoice_number","invoice_date","seller","buyer",
            "seller_siret","seller_tva","seller_iban",
            "currency","total_ht","total_tva","total_ttc"
        ]
        # colonnes “ligne”
        line_cols = ["line_ref","line_label","line_qty","line_unit_price","line_amount"]

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')

        # entête
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
            # au moins une ligne avec infos facture
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

        csv_text = '\ufeff' + out.getvalue()  # BOM pour Excel/Calc
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))
        csv_bytes.seek(0)
        return send_file(csv_bytes,
                         mimetype="text/csv; charset=utf-8",
                         as_attachment=True,
                         download_name="billxpert_full.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --- Health ---
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})
