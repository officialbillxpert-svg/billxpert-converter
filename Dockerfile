# Dockerfile
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

# OS + Tesseract (fra+eng)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra \
        libjpeg62-turbo libpng16-16 \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# venv + PATH
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Code
COPY . .

# Tesseract v5 data
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

EXPOSE 10000

# IMPORTANT :
# - on passe par "bash -lc" uniquement pour l'expansion de ${PORT}
# - on appelle GUNICORN AVEC SON CHEMIN ABSOLU dans le venv
CMD ["bash","-lc","/opt/venv/bin/gunicorn app.main:app --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 120"]
