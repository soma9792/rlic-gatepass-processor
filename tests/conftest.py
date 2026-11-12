"""Shared test helpers: generate sample gate-pass PDFs at test time.

No real documents are stored in the repository. Each PDF is built with
PyMuPDF from the fake data below.
"""

import os
import sys

import pymupdf

# Make the project root importable (processor.py, app.py).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def gatepass_lines(staff_no="12345", name="JOHN DOE", qid="12345678901"):
    """Lines of text that look like an RLIC gate pass (fake data)."""
    return [
        "RLIC GATE PASS",
        f"Name: {name}",
        f"Staff No: {staff_no}",
        f"QID/Visa No: {qid}",
        "Company: ABC CONTRACTING CO. WLL",
        "Valid From: 01/01/2026",
        "Validity: 31/12/2026",
        "Authorized Areas: RL: 1, 2",
    ]


def make_pdf(path, pages):
    """Write a PDF with one page per item in `pages`.

    Each item is a list of text lines. Pages get a real text layer, so the
    OCR path is not needed.
    """
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 18
    doc.save(str(path))
    doc.close()
    return path
