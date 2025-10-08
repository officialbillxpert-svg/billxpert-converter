# Dockerfile
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

# OS deps + Tesseract (eng+fra)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra \
        libjpeg62-turbo libpng16-16 \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# très important pour Tesseract 5
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# EXPOSE facultatif sur Render (tu peux le laisser)
EXPOSE 10000

# ⚠️ Bind sur le PORT dynamique de Render
CMD ["bash","-lc","gunicorn app.main:app --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 120"]
