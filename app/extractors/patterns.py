from __future__ import annotations
import re

PATTERNS_VERSION = "v2025-10-02b"

NUM_RE   = re.compile(r'(?:Facture|Invoice|N[°o])\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
INVOICE_NUM_RE = re.compile(r'Num[ée]ro\s*[:#]?\s*([A-Z0-9\-\/\.]{3,})', re.I)
DATE_RE  = re.compile(r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})')

TOTAL_TTC_NEAR_RE = re.compile(
    r'(?:Total\s*(?:TTC)?|Grand\s*total|Total\s*amount|Total\s*à\s*payer)[^\n\r]*?([0-9][0-9\.\,\s]+)\s*€?',
    re.I
)
TOTAL_HT_NEAR_RE = re.compile(r'Total\s*HT[^\n\r]*?([0-9][0-9\.\,\s]+)\s*€?', re.I)
TVA_NEAR_RE      = re.compile(r'\bTVA\b[\s\S]{0,120}?([0-9][0-9\.\,\s]+)\s*€?', re.I)

EUR_STRICT_RE = re.compile(r'([0-9]+(?:[ \.,][0-9]{3})*(?:[\,\.][0-9]{2}))\s*€?')

SIRET_RE = re.compile(r'\b\d{14}\b')
SIREN_RE = re.compile(r'(?<!\d)\d{9}(?!\d)')
TVA_RE   = re.compile(r'\bFR[a-zA-Z0-9]{2}\s?\d{9}\b')
IBAN_RE  = re.compile(r'\bFR\d{2}(?:\s?\d{4}){3}\s?(?:\d{4}\s?\d{3}\s?\d{5}|\d{11})\b')

SELLER_BLOCK = re.compile(
    r'(?:Émetteur|Vendeur|Seller)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Client|Acheteur|Buyer)',
    re.I | re.S
)
CLIENT_BLOCK = re.compile(
    r'(?:Client|Acheteur|Buyer)\s*:?\s*(?P<blk>.+?)(?:\n{2,}|Émetteur|Vendeur|Seller)',
    re.I | re.S
)
# alias explicites si un template écrit “Émetteur / Destinataire”
EMETTEUR_BLOCK = SELLER_BLOCK
DESTINATAIRE_BLOCK = CLIENT_BLOCK

TABLE_HEADER_HINTS = [
    ("ref", "réf", "reference", "code"),
    ("désignation", "designation", "libellé", "description", "label"),
    ("qté", "qte", "qty", "quantité"),
    ("pu", "prix unitaire", "unit price"),
    ("montant", "total", "amount")
]

LINE_RX = re.compile(
    r'^(?P<ref>[A-Z0-9][A-Z0-9\-_/]{1,})\s+[—\-]\s+(?P<label>.+?)\s+'
    r'(?P<qty>\d{1,3})\s+(?P<pu>[0-9\.\,\s]+(?:€)?)\s+(?P<amt>[0-9\.\,\s]+(?:€)?)$',
    re.M
)

VAT_RATE_RE = re.compile(r'(?:TVA|VAT)\s*[:=]?\s*(20|10|5[.,]?5)\s*%?', re.I)

FOOTER_NOISE_PAT = re.compile(r'(merci|paiement|iban|file://|conditions|due date|bank|html)', re.I)
