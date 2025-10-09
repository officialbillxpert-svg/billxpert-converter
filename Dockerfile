# Dockerfile
FROM python:3.12-slim

# Dépendances système pour OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-fra poppler-utils \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

# Dossier de travail
WORKDIR /app

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir gunicorn

# Code
COPY . .

# Render fournit $PORT (ex: 10000)
ENV PYTHONUNBUFFERED=1
EXPOSE 10000

# Lance Gunicorn et bind sur le port Render
# (on utilise sh -c pour que $PORT soit expand)
CMD ["sh","-c","gunicorn wsgi:app -w 2 -k sync -b 0.0.0.0:$PORT"]