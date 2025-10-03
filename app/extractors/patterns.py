from __future__ import annotations
import re

# ---------- Version ----------
PATTERNS_VERSION = "v2025-10-03f"

# ---------- Numéro / Date ----------
FACTURE_NO_RE = re.compile(
    r'(?:FACTURE|Facture)\s*(?:N[°o]|No|Nº)\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-\/\.]{2,})',
    re.I
)
INVOICE_NUM_RE = re.compile(r'Num[ée]ro\s*[:#-]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
NUM_RE         = re.compile(r'(?:Facture|Invoice|N[°o]|No|Nº)\s*[:#-]?\s*([A-Z0-9\-\/\.]{3,})', re.I)

DATE_RE = re.compile(
    r'(\d{1,2}\s*[\/\-.]\s*\d{1,2}\s*[\/\-.]\s*\d{2,4}|\d{4}\s*[\/\-.]\s*\d{1,2}\s*[\/\-.]\s*\d{1,2})'
)

# ---------- Totaux ----------
TOTAL_TTC_NEAR_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Grand\s*total|Total\s*amount|Total\s*à\s*payer)\s*[:\-]?\s*[^\n\r]{0,60}?'
    r'([0-9][0-9\.\,\s]+)\s*€?',
    re.I
)
TOTAL_HT_NEAR_RE = re.compile(
    r'Total\s*HT\s*[:\-]?\s*[^\n\r]{0,60}?([0-9][0-9\.\,\s]+)\s*€?',
    re.I
)

# TVA (montant) — forcer € pour éviter de prendre "20" de "TVA 20%"
TVA_AMOUNT_NEAR_RE = re.compile(
    r'\bTVA\b[^\n\r]{0,120}?(?:\d{1,2}[.,]?\d?\s*%\s*[^\n\r]{0,30})?([0-9][0-9\.\,\s]+)\s*€',
    re.I
)
TVA_NEAR_RE = TVA_AMOUNT_NEAR_RE

EUR_STRICT_RE = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2}))\s*€?')

# ---------- Taux de TVA ----------
VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

# ---------- IDs FR ----------
SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

# ---------- Blocs parties ----------
SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Emetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer|Destinataire|DESTINATAIRE)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Emetteur|Vendeur|Seller)',
    re.I | re.S
)
EMETTEUR_BLOCK = re.compile(
    r'(?:^|\n)\s*(?:ÉMETTEUR|EMETTEUR)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|DESTINATAIRE|Client|Acheteur|Buyer)',
    re.I | re.S
)
DESTINATAIRE_BLOCK = re.compile(
    r'(?:^|\n)\s*(?:DESTINATAIRE)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|ÉMETTEUR|EMETTEUR|Seller|Vendeur)',
    re.I | re.S
)

# ---------- Lignes ----------
TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

FOOTER_NOISE_PAT = re.compile(r'(merci|paiement|iban|file://|conditions|due date|bank|html)', re.I)

LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

__all__ = [
    "PATTERNS_VERSION",
    "FACTURE_NO_RE", "INVOICE_NUM_RE", "NUM_RE", "DATE_RE",
    "TOTAL_TTC_NEAR_RE", "TOTAL_HT_NEAR_RE", "TVA_AMOUNT_NEAR_RE", "TVA_NEAR_RE",
    "EUR_STRICT_RE", "VAT_RATE_RE",
    "SIRET_RE", "SIREN_RE", "TVA_RE", "IBAN_RE",
    "SELLER_BLOCK", "CLIENT_BLOCK", "EMETTEUR_BLOCK", "DESTINATAIRE_BLOCK",
    "TABLE_HEADER_HINTS", "FOOTER_NOISE_PAT", "LINE_RX",
]
