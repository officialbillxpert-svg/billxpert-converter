# Dockerfile
FROM python:3.13-slim

# — Dépendances système nécessaires pour l’OCR et les conversions —
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    poppler-utils \
    libjpeg62-turbo \
    libpng16-16 \
    ghostscript \
 && rm -rf /var/lib/apt/lists/*

# Variables env utiles
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    PYTHONUNBUFFERED=1 \
    LC_ALL=C.UTF-8

WORKDIR /app

# — Dépendances Python —
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# — Code de l’app —
COPY . .

# Gunicorn avec un timeout un peu plus généreux (OCR peut être plus lent)
CMD ["gunicorn","app.main:app","--bind","0.0.0.0:${PORT:-8000}","--timeout","120","--workers","2","--threads","4"]
