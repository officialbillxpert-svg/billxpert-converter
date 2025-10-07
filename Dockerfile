# ---- base minime avec Tesseract installé une seule fois (caché) ----
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 LC_ALL=C.UTF-8

# OS deps + Tesseract (+ fra) + libs pour OpenCV/Pillow
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra libtesseract-dev \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
        libjpeg62-turbo libpng16-16 libwebp7 libopenjp2-7 zlib1g \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# ---- couche requirements (cache tant que requirements.txt ne change pas) ----
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- copie du code ----
COPY . .

# Tesseract v5 (Debian bookworm)
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# Render écoute sur ce port
EXPOSE 10000

# Démarrage
# app.main:app = module "app/main.py" exposant "app"
CMD ["gunicorn", "app.main:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--threads", "4", "--timeout", "120"]
