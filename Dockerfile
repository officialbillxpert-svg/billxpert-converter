# Dockerfile
FROM python:3.12-slim

# 1) Paquets système nécessaires (OCR + utils PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-fra poppler-utils libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Variables utiles
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 2) Dépendances Python
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Code
COPY . .

# 4) Port Render
ENV PORT=10000

# 5) Démarrage
CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:10000"]
