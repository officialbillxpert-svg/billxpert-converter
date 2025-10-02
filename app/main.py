# app/main.py
from __future__ import annotations
import os
import io
import csv
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

# --- Extractor ---
try:
    from .extractors.pdf_basic import extract_document as _extract
except Exception:
    from .extractors.pdf_basic import extract_pdf as _extract  # type: ignore

ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}

app = Flask(__name__)
CORS(app)

# --- DIAG TESSERACT ---
@app.get("/diag")
def diag():
    import shutil, subprocess
    try:
        import pytesseract
        pt_cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
    except Exception:
        pytesseract = None
        pt_cmd = None

    which = shutil.which("tesseract")
    langs, err = None, None
    try:
        out = subprocess.check_output(["tesseract","--list-langs"], stderr=subprocess.STDOUT, text=True)
        langs = [l.strip() for l in out.splitlines() if l.strip() and not l.lower().startswith("list of")]
    except Exception as e:
        err = str(e)

    return jsonify({
        "which_tesseract": which,
        "pytesseract_cmd": pt_cmd,
        "TESSDATA_PREFIX": os.environ.get("TESSDATA_PREFIX"),
        "langs": langs,
        "list_langs_error": err,
    })

def _save_upload_to_tmp() -> Path:
    if "file" not in request.files:
        raise ValueError("no_file")
    f = request.files["file"]
    if not f or not f.filename:
        raise ValueError("no_file")

    filename = secure_filename(f.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"unsupported_ext:{ext}")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    f.save(tmp)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)

def _json_ok(data: Dict[str, Any], status: int = 200):
    resp = jsonify(data)
    resp.status_code = status
    return resp

def _json_err(error: str, details: str = "", status: int = 400):
    return _json_ok({"success": False, "error": error, "details": details}, status=status)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/api/convert")
def api_convert():
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        data = _extract(str(path), ocr="auto")
        return _json_ok(data, 200)
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try: os.unlink(path)
        except Exception: pass

@app.post("/api/summary")
def api_summary():
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        data = _extract(str(path), ocr="auto") or {}
        fields = data.get("fields") or {}
        flat = {
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
            "currency":       fields.get("currency"),
            "lines_count":    fields.get("lines_count"),
            # debug
            "line_strategy":  (data.get("meta") or {}).get("line_strategy"),
            "ocr_used":       (data.get("meta") or {}).get("ocr_used"),
        }
        if not data.get("success") and data.get("error"):
            flat.update({"_error": data.get("error"), "_details": data.get("details")})
        return _json_ok(flat, 200)
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try: os.unlink(path)
        except Exception: pass

@app.post("/api/summary.csv")
def api_summary_csv():
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        data = _extract(str(path), ocr="auto") or {}
        fields = data.get("fields") or {}
        flat = {
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
            "currency":       fields.get("currency"),
            "lines_count":    fields.get("lines_count"),
            "line_strategy":  (data.get("meta") or {}).get("line_strategy"),
            "ocr_used":       (data.get("meta") or {}).get("ocr_used"),
        }
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(flat.keys()), delimiter=';', extrasaction="ignore")
        writer.writeheader()
        writer.writerow(flat)
        mem = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="billxpert_summary.csv")
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try: os.unlink(path)
        except Exception: pass

@app.post("/api/lines")
def api_lines():
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        data = _extract(str(path), ocr="auto") or {}
        lines = data.get("lines") or []
        return _json_ok({
            "count": len(lines),
            "strategy": (data.get("meta") or {}).get("line_strategy"),
            "lines": lines
        }, 200)
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try: os.unlink(path)
        except Exception: pass

@app.post("/api/lines.csv")
def api_lines_csv():
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        data = _extract(str(path), ocr="auto") or {}
        rows: List[Dict[str, Any]] = data.get("lines") or []
        fieldnames = ["ref", "label", "qty", "unit_price", "amount"]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=';', extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "ref":        r.get("ref"),
                "label":      r.get("label"),
                "qty":        r.get("qty"),
                "unit_price": r.get("unit_price"),
                "amount":     r.get("amount"),
            })
        mem = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="billxpert_lines.csv")
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try: os.unlink(path)
        except Exception: pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
