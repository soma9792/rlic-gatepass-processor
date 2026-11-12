# RLIC Gate Pass Processor

A small web app that takes RLIC gate-pass PDFs and:

1. Splits multi-page PDFs into single pages.
2. Reads each page (PDF text layer first, OCR for scanned pages).
3. Renames each PDF by its **Staff No.**
4. Writes an Excel summary of the extracted fields.
5. Returns everything as **one ZIP** download.

Users select files from their own computer in the browser. The server does not
read or write any shared or network drive.

## How it stays safe

- **Nothing is kept on the server.** Each request gets its own temporary folder.
  The folder is deleted as soon as processing finishes, before the ZIP is sent.
  The ZIP is built in memory, so there is no file to clean up after download.
- **PDF only.** A file must end in `.pdf` *and* contain the `%PDF-` header.
  Files that fail either check are rejected with a clear message.
- **Limits** (defaults; set by environment variables, see below):

  | Limit | Default | Variable |
  |---|---|---|
  | Files per batch | 30 | `RLIC_MAX_FILES` |
  | Size per file | 20 MB | `RLIC_MAX_FILE_MB` |
  | Total upload per batch | 60 MB | `RLIC_MAX_TOTAL_MB` |
  | Pages per batch | 300 | `RLIC_MAX_TOTAL_PAGES` |

- **Safe file names.** Uploaded names go through `secure_filename`. Staff No.
  values keep only letters, digits, `-` and `_` before becoming file names.
- **No document content in logs.** The server logs file counts, page counts and
  timings. It logs exception types, not messages, and never file names or
  extracted text.
- **Password-protected PDFs** are rejected.
- **One batch at a time.** OCR is heavy, so batches are processed one at a
  time and other requests wait. This also avoids running the OCR model in
  parallel.

Limitations to know about:

- The server runs the code you deploy. Anyone who can reach the site can send
  files for processing. Put it behind your own access control (for example,
  your host's login or SSO) if the documents are confidential.
- Files are processed in server memory and on the server's temporary disk while
  a request runs. Use HTTPS in production.

## Project layout

```
app.py              Flask app: upload checks, per-request temp folder, ZIP response
processor.py        Text extraction, field parsing, page split, rename, Excel export
templates/index2.html   Upload page
requirements.txt        Core dependencies (text-layer PDFs)
requirements-ocr.txt    Optional OCR dependencies for scanned PDFs (large)
Dockerfile              Production image (gunicorn, CPU PyTorch, EasyOCR models baked in)
tests/                  pytest suite; sample PDFs are generated during the tests
```

## Run locally

Tested with Python 3.12. The Docker image uses Python 3.12.

Windows (PowerShell):

```bash
cd "rlic_web_app_public"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

macOS / Linux:

```bash
cd rlic_web_app_public
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 in your browser.

The core install handles PDFs with a text layer. To also handle **scanned**
PDFs, install the OCR extras. This pulls in PyTorch (several hundred MB to
over 1 GB depending on platform):

```bash
pip install -r requirements-ocr.txt
```

The English OCR models (about 100 MB) download on the first scanned-PDF
request, so that first request is slow.

To change the host or port for local runs:

```bash
RLIC_HOST=0.0.0.0 RLIC_PORT=8000 python app.py
```

(On Windows PowerShell: `$env:RLIC_PORT=8000; python app.py`.)

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

The tests generate sample gate-pass PDFs with PyMuPDF and cover: field
parsing, renaming, page splitting, duplicate Staff No., Excel export, ZIP
contents, temp-folder cleanup, PDF header and extension checks, size, count
and page limits, password-protected files, unsafe names, the missing-OCR
error, and that logs do not contain document text.

## Run with Docker

Build (the first build downloads PyTorch and the OCR models and takes a while):

```bash
docker build -t rlic-processor .
```

Run:

```bash
docker run --rm -p 8000:8000 rlic-processor
```

Open http://localhost:8000.

Image size: roughly **1.5–2 GB**, mostly PyTorch (CPU build) and the OCR
models (about 100 MB). The Dockerfile sets `EASYOCR_MODULE_PATH=/opt/easyocr` so
the models are baked into the image and the first scanned PDF does not download
them.

## Deploy

Any host that runs a Docker image will work (Render, Fly.io, Azure Container
Apps, Google Cloud Run, a company server, etc.). The image listens on the port
in the `PORT` environment variable (default 8000).

Resource guidance:

- **Memory:** at least 2 GB RAM. Scanned PDFs load the OCR model (several
  hundred MB in memory). Small free tiers (512 MB) will fail on scanned input.
- **Request timeout:** set it to at least 15 minutes for large scanned batches.
  The gunicorn timeout is already 900 s, but the platform's own proxy timeout
  also applies. Some hosts cap this at 60 s or 300 s.
- **Cold start:** the first request after a deploy or a scale-up loads the
  model. Expect several seconds for text-layer PDFs and longer for the first
  scanned PDF, since EasyOCR loads its models into memory on first use.
- **Instances:** run **one** instance (or one worker). Each instance holds its
  own copy of the OCR model, and batches are serialized inside an instance.

Environment variables you can set on the host:

| Variable | Purpose | Default |
|---|---|---|
| `PORT` | Port gunicorn listens on (Docker image) | `8000` |
| `RLIC_MAX_FILES` | Max files per batch | `30` |
| `RLIC_MAX_FILE_MB` | Max size per file (MB) | `20` |
| `RLIC_MAX_TOTAL_MB` | Max total upload per batch (MB) | `60` |
| `RLIC_MAX_TOTAL_PAGES` | Max pages per batch | `300` |

## Notes on the rules

- Company names are cut after the first legal suffix, as in the original tool
  (for example, `ABC CONTRACTING CO. WLL` is stored as `ABC CONTRACTING CO`).
- A file with no readable text is listed in the Excel file with the error
  "No text extracted" and keeps its original name.
- Duplicate Staff Nos. become `12345.pdf`, `12345_1.pdf`, and so on.

## Not verified yet

- The EasyOCR path on real scanned PDFs was not run in the development
  environment (EasyOCR and PyTorch were not installed there). The code path
  is covered only by a test that simulates "OCR unavailable".
- The Dockerfile has not been built in this environment (no Docker available).
- The application has not been deployed to any hosting provider.
