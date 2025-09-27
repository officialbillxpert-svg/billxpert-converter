from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil, re

# Imports projet
from .extractors.pdf_basic import extract_pdf
from .extractors.summary import summarize_from_text  # summarize_from_csv pas utilisé ici

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
  <button type="submit">Voir JSON</button>
</form>
<p style="margin-top:12px">Exporter les lignes :</p>
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

def _flat(s: str) -> str:
    """Compresse les espaces et enlève les sauts de ligne / points-virgules."""
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    s = s.replace(";", ",")  # éviter de casser le CSV au ';'
    return s

def _build_summary_from_data(data: dict) -> dict:
    data = data or {}
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

    # Compléter via heuristiques texte si trous
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

    # Aplatis les champs texte pour un CSV propre
    for k in ("seller", "buyer"):
        if summary.get(k):
            summary[k] = _flat(summary[k])

    return summary

# === JSON brut de l'extracteur ===
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

# === CSV résumé (1 ligne) ===
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
        fields = (data or {}).get("fields", {}) or {}

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n', quoting=csv.QUOTE_ALL)
        w.writerow(["invoice_number", "seller", "buyer", "total", "currency"])
        w.writerow([
            _flat(fields.get("invoice_number", "")),
            _flat(fields.get("seller", "N/A")),
            _flat(fields.get("buyer", "N/A")),
            fields.get("total_ttc", ""),
            fields.get("currency", "EUR"),
        ])

        csv_text = '\ufeff' + out.getvalue()
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

# === JSON résumé ===
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

# === CSV résumé (toutes les colonnes du résumé) ===
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

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n', quoting=csv.QUOTE_ALL)
        headers = list(summary.keys())
        w.writerow(headers)
        w.writerow([_flat(summary.get(k, "")) for k in headers])

        csv_text = '\ufeff' + out.getvalue()
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

# === CSV des LIGNES d’articles ===
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
        fields = data.get("fields", {}) or {}
        lines  = data.get("lines", []) or []

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n', quoting=csv.QUOTE_ALL)
        w.writerow(["invoice_number","invoice_date","currency","seller","buyer","ref","label","qty","unit_price","amount"])
        for r in lines:
            w.writerow([
                _flat(fields.get("invoice_number","")),
                _flat(fields.get("invoice_date","")),
                _flat(fields.get("currency","EUR")),
                _flat(fields.get("seller","")),
                _flat(fields.get("buyer","")),
                _flat(r.get("ref","")),
                _flat(r.get("label","")),
                r.get("qty",""),
                r.get("unit_price",""),
                r.get("amount",""),
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

# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
