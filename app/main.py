# app/main.py
from __future__ import annotations
import os
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

    try:
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        (Path(app.instance_path) / "uploads").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    @app.get("/")
    def root():
        return jsonify({"ok": True, "service": "billxpert-converter", "path": "/"}), 200

    @app.get("/health")
    def health():
        return jsonify({"ok": True}), 200

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "billxpert-converter"}), 200

    @app.get("/debug/info")
    def debug_info():
        import shutil, sys
        bins = {
            "tesseract": shutil.which("tesseract") or "",
            "pdftoppm": shutil.which("pdftoppm") or "",
            "python": sys.executable,
            "port": os.getenv("PORT", ""),
        }
        return jsonify({"ok": True, "bins": bins}), 200

    @app.post("/summary")
    def api_summary():
        try:
            file = request.files.get("file")
            if not file or not getattr(file, "filename", ""):
                return _json_err("bad_request", "Aucun fichier reçu", 400)
            ext = Path(file.filename).suffix.lower()
            if ext not in ALLOWED_EXTS:
                return _json_err("unsupported_type", f"Extension non supportée: {ext}", 415)

            tmp = Path(app.instance_path) / "uploads"
            tmp.mkdir(parents=True, exist_ok=True)
            safe_name = secure_filename(file.filename)
            dest = tmp / safe_name
            file.save(dest)

            ocr_mode = (request.args.get("ocr") or "auto").lower()         # auto | force | off
            engine   = (request.args.get("engine") or "auto").lower()      # auto | tesseract | paddle

            data = extract_document(str(dest), ocr=ocr_mode, engine=engine) or {}
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

try:
    app  # type: ignore
except NameError:
    app = create_app()