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
    # nouvelle API
    from .extractors.pdf_basic import extract_document as _extract
except Exception:
    # alias rétro-compatibilité
    from .extractors.pdf_basic import extract_pdf as _extract  # type: ignore

ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}

app = Flask(__name__)
CORS(app)


def _save_upload_to_tmp() -> Path:
    """Sauvegarde l’upload multipart dans /tmp en conservant l’extension.
    Lève une ValueError si pas de fichier ou ext non supportée.
    """
    if "file" not in request.files:
        raise ValueError("no_file")
    f = request.files["file"]
    if not f or not f.filename:
        raise ValueError("no_file")

    filename = secure_filename(f.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"unsupported_ext:{ext}")

    # IMPORTANT: garder l’extension pour que l’extractor sache si c’est une image
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
    """JSON brut complet (contient .fields, .lines, .meta)."""
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        # OCR auto: images -> OCR ; PDF texte ; PDF scanné -> signale s’il manque tesseract
        data = _extract(str(path), ocr="auto")
        # Même si success=False, on renvoie 200 avec un JSON propre (le front affichera le msg)
        return _json_ok(data, 200)
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


@app.post("/api/summary")
def api_summary():
    """Résumé JSON à plat pour les colonnes principales."""
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
        }
        # si l’extractor signale une erreur OCR, on te la renvoie aussi
        if not data.get("success") and data.get("error"):
            flat.update({"_error": data.get("error"), "_details": data.get("details")})
        return _json_ok(flat, 200)
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


@app.post("/api/summary.csv")
def api_summary_csv():
    """Résumé CSV (1 ligne)."""
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
        }
        # CSV en mémoire
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(flat.keys()), delimiter=';', extrasaction="ignore")
        writer.writeheader()
        writer.writerow(flat)
        mem = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))  # BOM UTF-8
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="billxpert_summary.csv")
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


@app.post("/api/lines")
def api_lines():
    """Retourne {count, lines: [...]}. Accepte PDF/JPG/PNG."""
    try:
        path = _save_upload_to_tmp()
    except ValueError as e:
        return _json_err("bad_request", str(e), 400)
    try:
        data = _extract(str(path), ocr="auto") or {}
        lines = data.get("lines") or []
        return _json_ok({"count": len(lines), "lines": lines}, 200)
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


@app.post("/api/lines.csv")
def api_lines_csv():
    """CSV des lignes d’articles."""
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
        mem = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))  # BOM UTF-8
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="billxpert_lines.csv")
    except Exception as e:
        return _json_err("server_error", f"{type(e).__name__}: {e}", 500)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
