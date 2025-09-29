# app/main.py
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil

from .extractors.pdf_basic import extract_pdf

app = Flask(__name__)
CORS(app)

HTML_FORM = """
<!doctype html><meta charset="utf-8">
<title>BillXpert Converter — Test</title>
<h1>BillXpert Converter — Test</h1>
<form method="post" action="/api/convert.csv" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Exporter CSV (résumé)</button>
</form>
<p style="margin-top:12px">Ou test JSON :</p>
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

def _summary_from_result(data: dict) -> dict:
    d = data or {}
    f = d.get("fields", {})
    return {
        "invoice_number": f.get("invoice_number"),
        "invoice_date":   f.get("invoice_date"),
        "seller":         f.get("seller"),
        "seller_siret":   f.get("seller_siret"),
        "seller_tva":     f.get("seller_tva"),
        "seller_iban":    f.get("seller_iban"),
        "buyer":          f.get("buyer"),
        "total_ht":       f.get("total_ht"),
        "total_tva":      f.get("total_tva"),
        "total_ttc":      f.get("total_ttc"),
        "currency":       f.get("currency"),
        "lines_count":    f.get("lines_count"),
    }

# ---------- API ----------

@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    ocr_mode = request.args.get("ocr", "auto")
    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path, ocr=ocr_mode)
        return jsonify(data)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.post("/api/convert.csv")
def api_convert_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    ocr_mode = request.args.get("ocr", "auto")
    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path, ocr=ocr_mode)
        s = _summary_from_result(data)

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["invoice_number","invoice_date","seller","buyer","total_ht","total_tva","total_ttc","currency","lines_count"])
        w.writerow([s.get("invoice_number",""), s.get("invoice_date",""), s.get("seller",""),
                    s.get("buyer",""), s.get("total_ht",""), s.get("total_tva",""),
                    s.get("total_ttc",""), s.get("currency","EUR"), s.get("lines_count","")])

        csv_text = '\ufeff' + out.getvalue()
        bio = io.BytesIO(csv_text.encode("utf-8"))
        bio.seek(0)
        return send_file(bio, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name="billxpert_convert.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.post("/api/summary")
def api_summary():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    ocr_mode = request.args.get("ocr", "auto")
    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path, ocr=ocr_mode)
        return jsonify(_summary_from_result(data))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.post("/api/summary.csv")
def api_summary_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    ocr_mode = request.args.get("ocr", "auto")
    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path, ocr=ocr_mode)
        s = _summary_from_result(data)
        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(s.keys())
        w.writerow([s.get(k,"") for k in s.keys()])
        csv_text = '\ufeff' + out.getvalue()
        bio = io.BytesIO(csv_text.encode("utf-8")); bio.seek(0)
        return send_file(bio, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name="billxpert_summary.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.post("/api/lines")
def api_lines():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    ocr_mode = request.args.get("ocr", "auto")
    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path, ocr=ocr_mode) or {}
        lines = data.get("lines") or []
        return jsonify({"count": len(lines), "lines": lines})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.post("/api/lines.csv")
def api_lines_csv():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    ocr_mode = request.args.get("ocr", "auto")
    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path, ocr=ocr_mode) or {}
        lines = data.get("lines") or []

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(["ref","label","qty","unit_price","amount"])
        for r in lines:
            w.writerow([r.get("ref",""), r.get("label",""), r.get("qty",""),
                        r.get("unit_price",""), r.get("amount","")])

        csv_text = '\ufeff' + out.getvalue()
        bio = io.BytesIO(csv_text.encode("utf-8")); bio.seek(0)
        return send_file(bio, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name="billxpert_lines.csv")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
