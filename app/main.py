from flask import Flask, request, jsonify, send_file, render_template_string, Response
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import tempfile, os, io, csv, shutil, json
from typing import Dict, Any

# Imports projet
from .extractors.pdf_basic import extract_pdf
from .extractors.summary import summarize_from_text  # summarize_from_csv pas utilisé ici

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Limite de taille (10 Mo) + JSON pour 413
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

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
<p style="margin-top:12px">Résumé :</p>
<form method="post" action="/api/summary" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Voir résumé (JSON)</button>
</form>
"""

@app.get("/")
def home():
    return render_template_string(HTML_FORM)

# ---------------------------------------------------------------------------
# Utilitaires de réponses JSON d’erreur uniformes
# ---------------------------------------------------------------------------

def json_error(status: int, code: str, message: str, extra: Dict[str, Any] | None = None):
    payload = {"success": False, "error": code, "message": message}
    if extra:
        payload.update(extra)
    return Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json; charset=utf-8")

@app.errorhandler(HTTPException)
def handle_http_exception(e: HTTPException):
    # Toutes les erreurs HTTP → JSON
    return json_error(e.code or 500, "http_error", e.description or str(e))

@app.errorhandler(413)
def handle_413(e):
    return json_error(413, "too_large", "Fichier trop volumineux (max 10 Mo).")

@app.errorhandler(Exception)
def handle_exception(e: Exception):
    # Toute autre exception non gérée → 500 JSON (sans stacktrace HTML)
    return json_error(500, "server_error", "Erreur interne lors du traitement.", {"detail": str(e)})

# ---------------------------------------------------------------------------

def _save_upload(file_storage):
    """Sauvegarde le fichier uploadé dans un répertoire temp et renvoie (path, tmpdir)."""
    tmpdir = tempfile.mkdtemp(prefix="bx_")
    path = os.path.join(tmpdir, file_storage.filename)
    file_storage.save(path)
    return path, tmpdir

def _safe_extract(path: str) -> Dict[str, Any]:
    """
    Appelle l’extracteur avec gestion d’exception pour toujours retourner du JSON.
    """
    try:
        data = extract_pdf(path)  # dict attendu
        if not isinstance(data, dict):
            raise ValueError("extract_pdf() doit renvoyer un dict")
        return data
    except Exception as e:
        # On laisse aussi l’erreur remonter au handler global,
        # mais ici on renvoie un dict "propre" si on souhaite l’utiliser.
        raise

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

    # Heuristiques texte pour compléter les trous
    raw_text = data.get("text") if isinstance(data, dict) else None
    if not raw_text:
        try:
            raw_text = " ".join(str(v) for v in fields.values() if v)
        except Exception:
            raw_text = ""

    if raw_text:
        try:
            auto = summarize_from_text(raw_text)
            if isinstance(auto, dict):
                for k, v in auto.items():
                    if summary.get(k) in (None, "", 0) and v not in (None, "", 0):
                        summary[k] = v
        except Exception:
            # On ignore si l’heuristique plante
            pass

    return summary

# === JSON brut de l’extracteur ===
@app.post("/api/convert")
def api_convert_json():
    if "file" not in request.files:
        return json_error(400, "file_missing", "Champ 'file' manquant.")
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return json_error(415, "not_pdf", "PDF uniquement.")

    path, tmpdir = _save_upload(f)
    try:
        data = _safe_extract(path)
        # Important: toujours JSON explicite (et charset) pour éviter tout "JSON brut" non parsé
        return Response(json.dumps(data, ensure_ascii=False), mimetype="application/json; charset=utf-8")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === CSV simple (quelques champs) ===
@app.post("/api/convert.csv")
def api_convert_csv():
    if "file" not in request.files:
        return json_error(400, "file_missing", "Champ 'file' manquant.")
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return json_error(415, "not_pdf", "PDF uniquement.")

    path, tmpdir = _save_upload(f)
    try:
        data = _safe_extract(path)
        fields = (data or {}).get("fields", {}) if isinstance(data, dict) else {}

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
        return json_error(400, "file_missing", "Champ 'file' manquant.")
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return json_error(415, "not_pdf", "PDF uniquement.")

    path, tmpdir = _save_upload(f)
    try:
        data = _safe_extract(path)
        summary = _build_summary_from_data(data)
        return Response(json.dumps(summary, ensure_ascii=False), mimetype="application/json; charset=utf-8")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# === Résumé CSV ===
@app.post("/api/summary.csv")
def api_summary_csv():
    if "file" not in request.files:
        return json_error(400, "file_missing", "Champ 'file' manquant.")
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return json_error(415, "not_pdf", "PDF uniquement.")

    path, tmpdir = _save_upload(f)
    try:
        data = _safe_extract(path)
        summary = _build_summary_from_data(data)

        out = io.StringIO()
        w = csv.writer(out, delimiter=';', lineterminator='\r\n')
        w.writerow(summary.keys())
        w.writerow([summary.get(k, "") for k in summary.keys()])

        csv_text = '\ufeff' + out.getvalue()  # BOM UTF-8
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

# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Run local : python -m app.main
    app.run(host="0.0.0.0", port=5000)
