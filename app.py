"""
RLIC Gate Pass Processor - public web app.

Flow for each request:
  1. The browser uploads PDF files (kept only in memory while the request runs).
  2. The server validates them (PDF header, size, count, page count).
  3. Each request gets its own temporary folder. Files are split, read,
     renamed by Staff No., and an Excel summary is written there.
  4. The renamed PDFs and the Excel file are packed into one ZIP in memory.
  5. The temporary folder is deleted BEFORE the ZIP is sent. Nothing from the
     request is left on disk, so there is nothing to clean up after download.
"""

import io
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime

import pymupdf as fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from processor import OCRUnavailableError, process_pdf_files

# ---- Safety limits (override with environment variables when hosting) ----
MAX_FILES = int(os.environ.get("RLIC_MAX_FILES", 30))
MAX_FILE_MB = int(os.environ.get("RLIC_MAX_FILE_MB", 20))
MAX_TOTAL_MB = int(os.environ.get("RLIC_MAX_TOTAL_MB", 60))
MAX_TOTAL_PAGES = int(os.environ.get("RLIC_MAX_TOTAL_PAGES", 300))
PDF_HEADER_SEARCH_BYTES = 1024  # the "%PDF-" marker must appear in the first 1 KB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rlic.app")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_TOTAL_MB * 1024 * 1024

# EasyOCR and the numpy/PyTorch stack are not guaranteed to be thread-safe,
# so processing runs one batch at a time. Uploads wait in line.
_processing_lock = threading.Lock()


class UploadError(Exception):
    """A problem with the uploaded files that the user can fix."""


def is_pdf_header(first_bytes):
    return b"%PDF-" in first_bytes[:PDF_HEADER_SEARCH_BYTES]


def read_validated_pdfs(files):
    """Validate uploaded files and return a list of (safe_name, bytes, pages).

    Raises UploadError with a user-facing message on the first problem.
    """
    uploads = [f for f in files if f and f.filename]
    if not uploads:
        raise UploadError("Please select at least one PDF file.")
    if len(uploads) > MAX_FILES:
        raise UploadError(f"Too many files. The limit is {MAX_FILES} files per batch.")

    max_bytes = MAX_FILE_MB * 1024 * 1024
    validated = []
    total_pages = 0

    for index, upload in enumerate(uploads, start=1):
        if not upload.filename.lower().endswith(".pdf"):
            raise UploadError("Only PDF files are accepted.")

        data = upload.stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise UploadError(f"A file is larger than {MAX_FILE_MB} MB.")
        if not is_pdf_header(data):
            raise UploadError("One of the files is not a valid PDF.")

        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            raise UploadError("One of the files is not a valid PDF.")
        try:
            if doc.needs_pass:
                raise UploadError("Password-protected PDFs are not supported.")
            pages = len(doc)
        finally:
            doc.close()

        total_pages += pages
        if total_pages > MAX_TOTAL_PAGES:
            raise UploadError(f"Too many pages. The limit is {MAX_TOTAL_PAGES} pages per batch.")

        # secure_filename strips paths and unsafe characters. Fall back to a
        # generic name if nothing is left, e.g. a name made only of symbols.
        safe_name = secure_filename(upload.filename) or f"upload_{index}.pdf"
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{safe_name}.pdf"
        validated.append((safe_name, data, pages))

    return validated


def unique_name(name, taken):
    """Return name, or name_2, name_3... if it is already used in this batch."""
    if name not in taken:
        return name
    stem, ext = os.path.splitext(name)
    counter = 2
    while f"{stem}_{counter}{ext}" in taken:
        counter += 1
    return f"{stem}_{counter}{ext}"


def build_zip(pdf_dir, results_dir):
    """Pack every PDF in pdf_dir and every Excel file in results_dir into a ZIP in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(os.listdir(pdf_dir)):
            if name.lower().endswith(".pdf"):
                archive.write(os.path.join(pdf_dir, name), arcname=f"renamed_pdfs/{name}")
        for name in sorted(os.listdir(results_dir)):
            if name.lower().endswith(".xlsx"):
                archive.write(os.path.join(results_dir, name), arcname=name)
    buffer.seek(0)
    return buffer


def run_batch(validated):
    """Process one batch in its own temporary folder and return the ZIP buffer.

    The temporary folder is always removed before this function returns.
    """
    work_dir = tempfile.mkdtemp(prefix="rlic_")
    try:
        pdf_dir = os.path.join(work_dir, "pdfs")
        results_dir = os.path.join(work_dir, "results")
        os.makedirs(pdf_dir)
        os.makedirs(results_dir)

        saved_names = []
        taken = set()
        for safe_name, data, _pages in validated:
            name = unique_name(safe_name, taken)
            taken.add(name)
            with open(os.path.join(pdf_dir, name), "wb") as handle:
                handle.write(data)
            saved_names.append(name)

        with _processing_lock:
            process_pdf_files(
                pdf_directory=pdf_dir,
                output_path=results_dir,
                target_files=saved_names,
                use_gpu=False,
            )

        return build_zip(pdf_dir, results_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index2.html",
        error=None,
        max_files=MAX_FILES,
        max_file_mb=MAX_FILE_MB,
        max_total_mb=MAX_TOTAL_MB,
        max_pages=MAX_TOTAL_PAGES,
    )


@app.route("/process", methods=["POST"])
def process():
    try:
        validated = read_validated_pdfs(request.files.getlist("pdfs"))
    except UploadError as exc:
        return _render_error(str(exc), 400)

    started = datetime.now()
    try:
        zip_buffer = run_batch(validated)
    except OCRUnavailableError:
        log.error("Scanned PDF received but EasyOCR is not installed")
        return _render_error("Scanned PDFs cannot be processed on this server.", 503)
    except Exception as exc:
        # Log the exception type only: messages and tracebacks can contain document text.
        log.error("Batch processing failed (%s)", type(exc).__name__)
        return _render_error("Processing failed. Please check the files and try again.", 500)

    log.info(
        "Processed %d file(s), %d page(s) in %.1fs",
        len(validated),
        sum(pages for _n, _d, pages in validated),
        (datetime.now() - started).total_seconds(),
    )

    download_name = f"RLIC_GatePass_Results_{datetime.now():%Y%m%d_%H%M%S}.zip"
    response = send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(RequestEntityTooLarge)
def too_large(_exc):
    return _render_error(f"The upload is larger than {MAX_TOTAL_MB} MB in total.", 413)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _render_error(message, status):
    return (
        render_template(
            "index2.html",
            error=message,
            max_files=MAX_FILES,
            max_file_mb=MAX_FILE_MB,
            max_total_mb=MAX_TOTAL_MB,
            max_pages=MAX_TOTAL_PAGES,
        ),
        status,
    )


if __name__ == "__main__":
    # Local development only. In Docker, gunicorn runs the app (see Dockerfile).
    host = os.environ.get("RLIC_HOST", "127.0.0.1")
    port = int(os.environ.get("RLIC_PORT", 5000))
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
