"""
RLIC gate-pass processing: text extraction, field parsing, page splitting,
renaming by Staff No., and Excel export.

This module works on whatever folders the caller passes in. It has no
hardcoded paths. Document contents are never logged; only file names and
exception types are.
"""

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime

import numpy as np
import pandas as pd
import pymupdf as fitz  # PyMuPDF

log = logging.getLogger("rlic.processor")

# OCR engine for scanned PDFs (pages with no text layer):
#   "tesseract" (default): the Tesseract program. Light on memory, fits free hosting.
#   "easyocr":             EasyOCR + PyTorch. Needs about 1-2 GB RAM. Optional.
#   "none":                turn OCR off; scanned PDFs get an error message.
OCR_ENGINE = os.environ.get("RLIC_OCR_ENGINE", "tesseract").strip().lower()
TESSERACT_LANG = os.environ.get("RLIC_TESSERACT_LANG", "eng")
TESSERACT_TIMEOUT_SECONDS = 120

# EasyOCR is imported lazily so the app and tests work without it.
_READER = None

COLUMNS_ORDER = [
    "Filename", "Pass Type", "QID/Visa No.", "Name", "Staff No.",
    "Company", "Valid From", "Valid Until", "Authorized Areas", "Errors",
]


class OCRUnavailableError(RuntimeError):
    """Raised when a scanned PDF needs OCR but EasyOCR is not installed."""


def get_ocr_reader(use_gpu=False):
    """EasyOCR reader (only used when RLIC_OCR_ENGINE=easyocr)."""
    global _READER
    if _READER is None:
        try:
            import easyocr  # noqa: WPS433 (lazy import on purpose)
        except ImportError as exc:
            raise OCRUnavailableError(
                "EasyOCR is not installed; scanned PDFs cannot be read."
            ) from exc
        _READER = easyocr.Reader(["en"], gpu=use_gpu)
    return _READER


def _pixmap_to_rgb_array(pix):
    """Convert a PyMuPDF pixmap to an RGB numpy array for EasyOCR."""
    if pix.n != 3 or pix.alpha:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)


def _ocr_page_tesseract(png_bytes):
    """Run the Tesseract program on one PNG image and return its text."""
    binary = shutil.which("tesseract")
    if not binary:
        raise OCRUnavailableError("Tesseract is not installed; scanned PDFs cannot be read.")
    result = subprocess.run(
        [binary, "stdin", "stdout", "-l", TESSERACT_LANG],
        input=png_bytes,
        capture_output=True,
        timeout=TESSERACT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        # Do not log stderr: it can echo parts of the document.
        raise RuntimeError(f"tesseract exited with code {result.returncode}")
    return result.stdout.decode("utf-8", errors="replace")


def _ocr_document(doc, use_gpu=False, dpi=300):
    """OCR every page of an open PyMuPDF document with the configured engine."""
    if OCR_ENGINE == "none":
        raise OCRUnavailableError("OCR is turned off on this server; scanned PDFs cannot be read.")

    if OCR_ENGINE == "easyocr":
        reader = get_ocr_reader(use_gpu=use_gpu)
        pages = []
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            results = reader.readtext(_pixmap_to_rgb_array(pix), detail=0, paragraph=True)
            pages.append("\n".join(results))
        return "\n".join(pages)

    # Default: Tesseract. Render each page to PNG, then OCR it one page at a time
    # so only one page image is in memory at once.
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        pages.append(_ocr_page_tesseract(pix.tobytes("png")))
        del pix
    return "\n".join(pages)


def extract_text_from_pdf(pdf_path, use_gpu=False, dpi=300):
    """Extract text from the PDF text layer; OCR pages that have none.

    Returns "" if the file cannot be opened or read.
    Raises OCRUnavailableError if OCR is needed but the engine is not available.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        log.warning("Could not open PDF %s (%s)", os.path.basename(pdf_path), type(exc).__name__)
        return ""

    try:
        text = ""
        for page in doc:
            page_text = page.get_text("text")
            if page_text:
                text += page_text + "\n"

        if text.strip():
            return text

        # No text layer at all: treat as scanned and OCR every page.
        return _ocr_document(doc, use_gpu=use_gpu, dpi=dpi)
    except OCRUnavailableError:
        raise
    except Exception as exc:
        log.warning("Could not read PDF %s (%s)", os.path.basename(pdf_path), type(exc).__name__)
        return ""
    finally:
        doc.close()


def clean_qid(qid):
    if not qid:
        return None
    qid = re.sub(r'[Oo]', '0', qid)
    qid = re.sub(r'[Il]', '1', qid)
    qid = re.sub(r'[^0-9]', '', qid)
    return qid


def validate_date(date_str):
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            datetime.strptime(date_str, fmt)
            return True
        except ValueError:
            continue
    return False


def clean_company_name(company):
    if not company:
        return None
    company = re.sub(r'^(Company|Employer)[:\s]*', '', company, flags=re.IGNORECASE)
    patterns = [
        r"^(.*?(?:CO\.?|WLL|SCJV|LLC|LTD|CORP|INC|CORPORATION)\b)",
        r"^(.*?\([^)]+\))",
        r"^([A-Z][A-Z\s&]+(?:CO|WLL|SCJV|LLC|LTD)?)"
    ]
    for pattern in patterns:
        match = re.match(pattern, company, re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
            return re.sub(r"[^a-zA-Z0-9&\s().,-]", "", cleaned)
    return company.strip()


def clean_authorized_areas(areas):
    if not areas:
        return None
    rl_match = re.search(r"(RL:\s*[\d,\s]+)", areas, re.IGNORECASE)
    if rl_match:
        return re.sub(r'\s+', ' ', rl_match.group(1)).strip()
    cleaned = re.sub(r"[^a-zA-Z0-9,\s:]", "", areas)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    meaningful = re.search(r"([A-Z]{2,}[\s:]*[\d,\s]+)", cleaned)
    return meaningful.group(1).strip() if meaningful else cleaned


def parse_rlic_gatepass(raw_text, filename):
    data = {
        "Filename": filename,
        "Pass Type": "RLIC Gate Pass",
        "QID/Visa No.": None,
        "Name": None,
        "Staff No.": None,
        "Company": None,
        "Valid From": None,
        "Valid Until": None,
        "Authorized Areas": None,
        "Errors": None
    }

    patterns = {
        "QID/Visa No.": [
            r"(?:QID[\s\/]*Visa(?:\s+No)?|Visa\s*No|QID)[\s\/\-:\n|]*([0-9]{9,11})",
            r"\b([0-9]{11})\b"
        ],
        "Staff No.": [
            r"(?:Staff|Employee|Emp)[\s#]*No[\s\n:|]*([A-Z0-9\-_]{4,10})",
            r"Staff\s*No[\s\n:|]*([^\n\r]+)",
            r"\b([0-9]{5,7})\b"
        ],
        "Name": [
            r"Name[\s\n:|]*([A-Z\s]{3,40})(?=\n|\r|Staff|Company|ID|QID|$)",
            r"(?:Full\s+)?Name[:\s]*([A-Z][A-Za-z\s]+?)(?=\n|Staff|Company|ID|$)"
        ],
        "Company": [
            r"(?:Company|Employer)[:\s]*(.+?)(?=\n|Authori[sz]ed|Valid|$)"
        ],
        "Authorized Areas": [
            r"Authori[sz]ed\s+Areas?[\s\n:]*(RL:?[\s\d,]+)",
            r"Authori[sz]ed\s+Areas?[:\s]*(.+?)(?=\n|Valid|$)"
        ]
    }

    for field, pattern_list in patterns.items():
        for pattern in pattern_list:
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                val = re.sub(r'^[|:\s]+|[|:\s]+$', '', val)
                if val:
                    data[field] = val
                    break

    if data["Staff No."]:
        data["Staff No."] = re.sub(r"\s+", "", data["Staff No."])

    errors = []

    if data["QID/Visa No."]:
        cleaned_qid = clean_qid(data["QID/Visa No."])
        if cleaned_qid and len(cleaned_qid) in [9, 11]:
            data["QID/Visa No."] = cleaned_qid
        else:
            errors.append(f"Invalid ID format: {data['QID/Visa No.']}")

    # Dates: prefer the labelled Validity / Valid From lines.
    validity_match = re.search(r"Validity[\s:|]*(\d{2}[\/\-\.]\d{2}[\/\-\.]\d{4})", raw_text, re.IGNORECASE)
    valid_from_match = re.search(r"Valid\s*From[\s:|]*(\d{2}[\/\-\.]\d{2}[\/\-\.]\d{4})", raw_text, re.IGNORECASE)

    if valid_from_match:
        data["Valid From"] = valid_from_match.group(1).replace("-", "/").replace(".", "/")
    if validity_match:
        data["Valid Until"] = validity_match.group(1).replace("-", "/").replace(".", "/")

    # Fallback to candidate dates if the labels were not matched.
    if not data["Valid From"] or not data["Valid Until"]:
        date_candidates = re.findall(r"\b(\d{2}[\/\-\.]\d{2}[\/\-\.]\d{4})\b", raw_text)
        if not data["Valid Until"] and len(date_candidates) >= 1:
            data["Valid Until"] = date_candidates[0].replace("-", "/").replace(".", "/")
        if not data["Valid From"] and len(date_candidates) >= 2:
            data["Valid From"] = date_candidates[1].replace("-", "/").replace(".", "/")

    if data["Valid From"] and not validate_date(data["Valid From"]):
        errors.append(f"Invalid From date: {data['Valid From']}")
    if data["Valid Until"] and not validate_date(data["Valid Until"]):
        errors.append(f"Invalid Until date: {data['Valid Until']}")

    data["Company"] = clean_company_name(data["Company"])
    data["Authorized Areas"] = clean_authorized_areas(data["Authorized Areas"])
    data["Errors"] = "; ".join(errors) if errors else None

    return data


def safe_staff_filename_stem(staff_no):
    """Keep only letters, digits, '-' and '_' so the result is a safe file name."""
    if not staff_no:
        return ""
    return re.sub(r"[^A-Za-z0-9_-]", "", staff_no)


def split_and_prepare_pdfs(pdf_directory, target_files):
    """
    Splits each multi-page PDF into single-page PDFs (<name>_page_N.pdf) and
    removes the original. Returns the list of single-page file names.
    """
    final_file_list = []

    for filename in target_files:
        if not filename.lower().endswith('.pdf'):
            continue

        file_path = os.path.join(pdf_directory, filename)
        if not os.path.exists(file_path):
            continue

        try:
            doc = fitz.open(file_path)
            num_pages = len(doc)

            if num_pages <= 1:
                doc.close()
                final_file_list.append(filename)
                continue

            base_name = os.path.splitext(filename)[0]
            for page_num in range(num_pages):
                new_doc = fitz.open()
                new_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)

                split_filename = f"{base_name}_page_{page_num + 1}.pdf"
                split_filepath = os.path.join(pdf_directory, split_filename)

                new_doc.save(split_filepath)
                new_doc.close()
                final_file_list.append(split_filename)

            doc.close()
            os.remove(file_path)

        except Exception as exc:
            log.warning("Page split failed for %s (%s)", filename, type(exc).__name__)
            final_file_list.append(filename)

    return final_file_list


def _empty_result(pdf_file, errors):
    row = {col: None for col in COLUMNS_ORDER}
    row["Filename"] = pdf_file
    row["Pass Type"] = "RLIC Gate Pass"
    row["Errors"] = errors
    return row


def process_pdf_files(pdf_directory, output_path, target_files=None, use_gpu=False):
    """
    Splits multi-page PDFs into single pages, extracts fields, renames each
    PDF to its Staff No., and writes an Excel summary to output_path.

    Returns the path of the Excel file, or None if there was nothing to process.
    """
    if target_files:
        raw_files = [f for f in target_files if f.lower().endswith('.pdf')]
    else:
        raw_files = [f for f in os.listdir(pdf_directory) if f.lower().endswith('.pdf')]

    if not raw_files:
        return None

    pdf_files = split_and_prepare_pdfs(pdf_directory, raw_files)

    all_results = []

    for pdf_file in pdf_files:
        pdf_path = os.path.join(pdf_directory, pdf_file)
        if not os.path.exists(pdf_path):
            continue

        text = extract_text_from_pdf(pdf_path, use_gpu=use_gpu, dpi=300)

        if not text.strip():
            all_results.append(_empty_result(pdf_file, "No text extracted"))
            continue

        result = parse_rlic_gatepass(text, pdf_file)

        # Rename the PDF to its Staff No.
        stem = safe_staff_filename_stem(result["Staff No."])
        if stem:
            ext = os.path.splitext(pdf_file)[1]
            new_filename = f"{stem}{ext}"
            new_pdf_path = os.path.join(pdf_directory, new_filename)

            if pdf_file != new_filename:
                # Duplicate Staff No.: add a counter suffix.
                counter = 1
                while os.path.exists(new_pdf_path):
                    new_filename = f"{stem}_{counter}{ext}"
                    new_pdf_path = os.path.join(pdf_directory, new_filename)
                    counter += 1

                shutil.move(pdf_path, new_pdf_path)
                result["Filename"] = new_filename

        all_results.append(result)

    df = pd.DataFrame(all_results, columns=COLUMNS_ORDER)

    os.makedirs(output_path, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_path, f"RLIC_GatePass_Results_{timestamp}.xlsx")
    df.to_excel(output_file, index=False)

    return output_file
