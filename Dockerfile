# Dockerfile
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

# --- Installation OS + Tesseract (fra + eng) ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra \
        libjpeg62-turbo libpng16-16 \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# --- Crée un environnement virtuel ---
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# --- Copie du projet ---
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .

# --- Variable importante pour Tesseract 5 ---
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# --- Port d’écoute (facultatif pour Render) ---
EXPOSE 10000

# --- Commande de lancement ---
# Render fournit une variable d'environnement $PORT automatiquement.
# On appelle Gunicorn via son chemin absolu pour éviter le "command not found"
CMD ["bash", "-lc", "/opt/venv/bin/gunicorn app.main:app --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 120"]
