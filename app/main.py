# app/main.py
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
import tempfile, os, io, csv, shutil

# Imports projet
from .extractors.pdf_basic import extract_pdf
from .extractors.summary import summarize_from_text  # utilisé pour compléter le résumé

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
    """Sauvegarde le fichier uploadé dans un répertoire temp et renvoie (path, tmpdir)."""
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    path = os.path.join(tmpdir, file_storage.filename)
    file_storage.save(path)
    return path, tmpdir

def _build_summary_from_data(data: dict) -> dict:
    """
    Construit le résumé à partir du dict retourné par extract_pdf(path),
    puis complète les champs manquants grâce à summarize_from_text() si possible.
    """
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

    # Heuristique texte pour compléter des trous (facultatif)
    raw_text = data.get("text") if isinstance(data, dict) else None
    if not raw_text:
        try:
            raw_text = " ".join(str(v) for v in fields.values() if v)
        except Exception:
            raw_text = ""
    if raw_text:
        try:
            auto = summarize_from_text(raw_text)
            for k, v in auto.items():
                if summary.get(k) in (None, "", 0) and v not in (None, "", 0):
                    summary[k] = v
        except Exception:
            pass

    return summary

# === JSON complet (avec lines) ===
@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path)  # -> dict: { success, meta, fields, lines?, text... }
        return jsonify(data)
    except Exception as e:
        return jsonify({"success": False, "error": "extract_failed", "detail": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === CSV (résumé très simple : 1 ligne) ===
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
        fields = (data or {}).get("fields", {})

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
    except Exception as e:
        return jsonify({"success": False, "error": "csv_failed", "detail": str(e)}), 500
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
        summary = _build_summary_from_data(data)
        return jsonify(summary)
    except Exception as e:
        return jsonify({"success": False, "error": "summary_failed", "detail": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Résumé CSV (colonnes du résumé) ===
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
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(summary.keys())
        w.writerow([summary.get(k, "") for k in summary.keys()])

        csv_text = '\ufeff' + out.getvalue()
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))
        csv_bytes.seek(0)

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_summary.csv"
        )
    except Exception as e:
        return jsonify({"success": False, "error": "summary_csv_failed", "detail": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === LIGNES — JSON dédié ===
@app.post("/api/lines.json")
def api_lines_json():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file_missing"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "not_pdf"}), 400

    path, tmpdir = _save_upload(f)
    try:
        data = extract_pdf(path) or {}
        lines = data.get("lines") or []
        return jsonify({"count": len(lines), "lines": lines})
    except Exception as e:
        return jsonify({"success": False, "error": "lines_json_failed", "detail": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === LIGNES — CSV dédié ===
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
        csv_bytes = io.BytesIO(csv_text.encode("utf-8"))
        csv_bytes.seek(0)

        return send_file(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="billxpert_lines.csv"
        )
    except Exception as e:
        return jsonify({"success": False, "error": "lines_csv_failed", "detail": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Run local : python -m app.main
    app.run(host="0.0.0.0", port=5000)
