# ---- base minime avec Tesseract installé ----
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 LC_ALL=C.UTF-8

# OS deps + tesseract (+ fra)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra \
        libjpeg62-turbo libpng16-16 \
        libglib2.0-0 libsm6 libxext6 libxrender1 \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

# Couche deps (cachée tant que requirements.txt ne change pas)
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Code
COPY . .

# Tesseract v5 (chemin de données)
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

EXPOSE 10000
CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120"]
