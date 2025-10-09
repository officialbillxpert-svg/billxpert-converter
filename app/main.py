
from __future__ import annotations
import io
from pathlib import Path
from typing import Any, Dict
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename

from app.extractors.pdf_basic import extract_document

ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}

def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.post("/summary")
    def api_summary():
        try:
            file = request.files.get("file")
            if not file or not getattr(file, "filename", ""):
                return _json_err("bad_request", "Aucun fichier reçu", 400)
            ext = Path(file.filename).suffix.lower()
            if ext not in ALLOWED_EXTS:
                return _json_err("unsupported_type", f"Extension non supportée: {ext}", 415)

            # save to a temp file
            tmp = Path(app.instance_path) / "uploads"
            tmp.mkdir(parents=True, exist_ok=True)
            safe_name = secure_filename(file.filename)
            dest = tmp / safe_name
            file.save(dest)

            ocr_mode = (request.args.get("ocr") or "auto").lower()
            data = extract_document(str(dest), ocr=ocr_mode) or {}
            fields: Dict[str, Any] = data.get("fields") or {}
            meta: Dict[str, Any] = data.get("meta") or {}

            flat = {
                "invoice_number": fields.get("invoice_number"),
                "invoice_date": fields.get("invoice_date"),
                "seller": fields.get("seller"),
                "buyer": fields.get("buyer"),
                "total_ht": fields.get("total_ht"),
                "total_tva": fields.get("total_tva"),
                "total_ttc": fields.get("total_ttc"),
            }
            return jsonify({"ok": True, "flat": flat, "fields": fields, "meta": meta})
        except Exception as e:
            return _json_err("internal_error", str(e), 500)

    return app

def _json_err(code: str, msg: str, status: int):
    return jsonify({"ok": False, "error": {"code": code, "message": msg}}), status
