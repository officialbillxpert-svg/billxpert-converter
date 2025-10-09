# Dockerfile
FROM python:3.12-slim

# Evite la création de .pyc et flush stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dépendances système pour l’OCR (Tesseract + Poppler)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-fra poppler-utils \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

# Dossier de travail
WORKDIR /app

# Dépendances Python
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt \
 && python -m pip check || true

# Code de l’app
COPY . .

# Render fournit $PORT (ex: 10000). On expose pour la doc.
EXPOSE 10000

# Gunicorn : 1 worker (boot plus rapide), logs d'accès/erreurs vers stdout
# ${PORT:-10000} permet de lancer aussi en local si PORT n'est pas défini
CMD ["sh","-c","gunicorn wsgi:app -w 1 --access-logfile - --error-logfile - -k sync -b 0.0.0.0:${PORT:-10000}"]