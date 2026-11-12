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
requirements-ocr.txt    Optional EasyOCR dependencies (large; not needed for Tesseract)
Dockerfile              Production image (gunicorn + Tesseract OCR)
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

Scanned PDFs (pages with no selectable text) are read with **Tesseract**. Install
the Tesseract program on your computer, then make sure `tesseract` runs from a
terminal:

- Windows: install from https://github.com/UB-Mannheim/tesseract/wiki and add
  `C:\Program Files\Tesseract-OCR` to your PATH.
- macOS: `brew install tesseract`
- Linux: `sudo apt-get install tesseract-ocr tesseract-ocr-eng`

Text-layer PDFs work without Tesseract. Without it, scanned PDFs get a clear
"cannot be read" message.

Optional: EasyOCR is still supported for better accuracy on hard scans. It
needs about 1–2 GB of RAM and PyTorch (`pip install -r requirements-ocr.txt`).
Select it with `RLIC_OCR_ENGINE=easyocr`.

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

Image size: about **300 MB**. The image includes the Tesseract program and English
language data, so no model download happens at runtime.

## Deploy

Any host that runs a Docker image will work (Render, Fly.io, Azure Container
Apps, Google Cloud Run, a company server, etc.). The image listens on the port
in the `PORT` environment variable (default 8000).

Resource guidance:

- **Memory:** Tesseract works page by page and runs as a separate program, so the
  free 512 MB plan on Render is enough. Each scanned page is rendered at 300 DPI
  and then released. Very large batches of scans are slower but use the same memory.
  If you switch to EasyOCR, you need 2 GB RAM or more.
- **Request timeout:** scanned batches can take minutes. The gunicorn timeout
  is 900 s, but the platform's proxy timeout also applies, and some hosts cap it
  lower.
- **Cold start:** free plans sleep when idle. The first visit after that can take
  about a minute.
- **Instances:** run **one** instance. Batches are serialized inside an instance.

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

- Tesseract was tested on a synthetic scanned page (one image, no text layer).
  Accuracy on real gate-pass scans has not been checked. Test with real files
  and compare the Staff No. column with the originals.
- EasyOCR is not tested in the current setup.
- The Dockerfile was not built in the development environment (no Docker).
- The Render deployment was checked with a health request and the home page. A
  scanned PDF on the free plan ran out of memory with EasyOCR. Re-test with
  Tesseract.
