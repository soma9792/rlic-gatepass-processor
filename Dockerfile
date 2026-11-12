# RLIC Gate Pass Processor - production image.
#
# Scanned PDFs are read with the Tesseract program (installed below), which is
# light enough for Render's free plan (512 MB RAM). Image size is about 300 MB.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RLIC_OCR_ENGINE=tesseract

WORKDIR /app

# 1) Tesseract OCR with English language data.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# 2) Python dependencies.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 3) Application code, run as a non-root user.
COPY processor.py app.py ./
COPY templates ./templates
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# One worker and threads for concurrent requests. Processing itself is
# serialized by a lock in app.py. The timeout is long because OCR on
# scanned PDFs can take minutes.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4 --timeout 900 --graceful-timeout 30 app:app"]
