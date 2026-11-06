FROM python:3.12-slim-bookworm

# Tesseract language packs to install (space separated Tesseract codes).
# Add more with: docker compose build --build-arg OCR_LANGS="eng deu jpn chi-sim"
ARG OCR_LANGS="eng deu fra spa ita por nld"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-osd ghostscript \
      $(for l in $OCR_LANGS; do echo "tesseract-ocr-$l"; done) \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
