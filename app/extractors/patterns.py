# app/extractors/patterns.py
from __future__ import annotations
import re

PATTERNS_VERSION = "v1.0.0"

DATE_RE = re.compile(r"(?:^|[^0-9])((?:\d{1,2}[./-]){2}\d{2,4}|\d{4}-\d{2}-\d{2})(?:$|[^0-9])")
FACTURE_NO_RE = re.compile(r"(?:facture\s*(?:n°|no|num(?:éro)?|#)?\s*[:\-\s]*|invoice\s*(?:no|#)?\s*[:\-\s]*)([A-Za-z0-9._\-/]+)", re.IGNORECASE)
INVOICE_NUM_RE = re.compile(r"(?:\b(?:n°|no|num(?:éro)?|#)\s*[:\-\s]*)([A-Za-z0-9._\-/]+)", re.IGNORECASE)

EUR_STRICT_RE = re.compile(r"\b\d{1,3}(?:[\s\u00A0.]\d{3})*(?:[.,]\d{2})\b")
TOTAL_TTC_NEAR_RE = re.compile(r"(total\s*t\s*t\s*c|ttc|total\s*ttc|montant\s*ttc)", re.IGNORECASE)
TOTAL_HT_NEAR_RE  = re.compile(r"(total\s*h\s*t|ht|total\s*ht|montant\s*ht)", re.IGNORECASE)
TVA_AMOUNT_NEAR_RE = re.compile(r"(tva|taxe\s*\b|vat)", re.IGNORECASE)

VAT_RATE_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?\s*%)")

SELLER_BLOCK = re.compile(r"(?s)(?:vendeur|société|entreprise|emetteur|from)\s*[:\n]+(.{10,300})", re.IGNORECASE)
CLIENT_BLOCK = re.compile(r"(?s)(?:client|destinataire|acheteur|to)\s*[:\n]+(.{10,300})", re.IGNORECASE)
EMETTEUR_BLOCK = re.compile(r"(?s)(?:émetteur|emetteur)\s*[:\n]+(.{10,300})", re.IGNORECASE)
DESTINATAIRE_BLOCK = re.compile(r"(?s)(?:destinataire)\s*[:\n]+(.{10,300})", re.IGNORECASE)

TVA_RE   = re.compile(r"\bTVA\b\s*:?\s*([A-Z0-9\s]+)", re.IGNORECASE)
SIRET_RE = re.compile(r"\bSIRET\b\s*:?\s*(\d{14})", re.IGNORECASE)
SIREN_RE = re.compile(r"\bSIREN\b\s*:?\s*(\d{9})", re.IGNORECASE)
IBAN_RE  = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
