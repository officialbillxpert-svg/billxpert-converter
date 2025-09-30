# Dockerfile
FROM python:3.13-slim

# Evite les prompts
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dépendances système pour Tesseract + Poppler pour PDF -> images
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    libtesseract-dev \
    poppler-utils \
    gcc \
  && rm -rf /var/lib/apt/lists/*

# Dossier app
WORKDIR /app
COPY requirements.txt /app/

# Dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Code
COPY . /app

# Port Render
ENV PORT=10000
CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:10000"]
