# RLIC Gate Pass Processor - production image.
#
# Image size: roughly 1.5-2 GB with the CPU build of PyTorch and EasyOCR,
# most of it PyTorch. The English OCR models (~100 MB) are downloaded at
# build time, so the first scanned-PDF request does not wait for a download.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EASYOCR_MODULE_PATH=/opt/easyocr

WORKDIR /app

# 1) CPU-only PyTorch (the default wheel pulls in CUDA and is several GB).
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# 2) App and OCR dependencies.
COPY requirements.txt requirements-ocr.txt ./
RUN pip install -r requirements.txt -r requirements-ocr.txt

# 3) Pre-download the EasyOCR English models into the image.
RUN python -c "import easyocr; easyocr.Reader(['en'], gpu=False)" \
    && chmod -R a+rX /opt/easyocr

# 4) Application code, run as a non-root user.
COPY processor.py app.py ./
COPY templates ./templates
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# One worker keeps one copy of the OCR model in memory. Threads let the
# server accept requests while another batch runs (processing itself is
# serialized by a lock in app.py). The timeout is long because OCR on
# scanned PDFs can take minutes.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4 --timeout 900 --graceful-timeout 30 app:app"]
