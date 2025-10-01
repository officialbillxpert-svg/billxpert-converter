# Dockerfile
FROM python:3.12-slim

# 1) Paquets système nécessaires
# - tesseract-ocr + fra : OCR FR
# - qpdf : runtime pikepdf
# - libglib2.0-0 : dépendance fréquente pillow/pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-fra \
    qpdf libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# 2) Variables d'env utiles
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# 3) Dépendances Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) Code
COPY . .

# 5) Port Render
ENV PORT=10000

# 6) Démarrage
CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:10000"]
