from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv
from extractors.pdf_basic import extract_pdf

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
