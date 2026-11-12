import os
import re
import shutil
import sys

import pandas as pd
import pymupdf
import pytest

import processor
from processor import (
    OCRUnavailableError,
    parse_rlic_gatepass,
    process_pdf_files,
    safe_staff_filename_stem,
)
from tests.conftest import gatepass_lines, make_pdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _EmptyReader:
    """Stands in for EasyOCR: finds no text on any page."""

    def readtext(self, *_args, **_kwargs):
        return []


@pytest.fixture
def no_ocr_text(monkeypatch):
    """Use a fake OCR engine that finds no text, so blank pages stay blank."""
    monkeypatch.setattr(processor, "OCR_ENGINE", "easyocr")
    monkeypatch.setattr(processor, "get_ocr_reader", lambda use_gpu=False: _EmptyReader())


def test_parse_extracts_fields():
    text = "\n".join(gatepass_lines(staff_no="12345", name="JOHN DOE", qid="12345678901"))

    data = parse_rlic_gatepass(text, "sample.pdf")

    assert data["Staff No."] == "12345"
    assert data["QID/Visa No."] == "12345678901"
    assert data["Name"] == "JOHN DOE"
    # Existing rule: company is cut after the first legal suffix ("CO").
    assert data["Company"] == "ABC CONTRACTING CO"
    assert data["Valid From"] == "01/01/2026"
    assert data["Valid Until"] == "31/12/2026"
    assert data["Authorized Areas"] == "RL: 1, 2"
    assert data["Errors"] is None


def test_process_renames_by_staff_no_and_writes_excel(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    results = tmp_path / "results"
    make_pdf(pdf_dir / "upload.pdf", [gatepass_lines(staff_no="12345")])

    excel = process_pdf_files(str(pdf_dir), str(results), ["upload.pdf"])

    assert (pdf_dir / "12345.pdf").exists()
    assert not (pdf_dir / "upload.pdf").exists()
    assert excel is not None and os.path.dirname(excel) == str(results)

    df = pd.read_excel(excel, dtype=str)
    assert list(df.columns) == [
        "Filename", "Pass Type", "QID/Visa No.", "Name", "Staff No.",
        "Company", "Valid From", "Valid Until", "Authorized Areas", "Errors",
    ]
    assert df.loc[0, "Filename"] == "12345.pdf"
    assert df.loc[0, "Staff No."] == "12345"


def test_multi_page_pdf_is_split_and_each_page_renamed(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    make_pdf(
        pdf_dir / "batch.pdf",
        [gatepass_lines(staff_no="11111"), gatepass_lines(staff_no="22222")],
    )

    process_pdf_files(str(pdf_dir), str(tmp_path / "results"), ["batch.pdf"])

    assert not (pdf_dir / "batch.pdf").exists()
    assert (pdf_dir / "11111.pdf").exists()
    assert (pdf_dir / "22222.pdf").exists()


def test_duplicate_staff_numbers_get_counter_suffix(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    make_pdf(pdf_dir / "a.pdf", [gatepass_lines(staff_no="33333")])
    make_pdf(pdf_dir / "b.pdf", [gatepass_lines(staff_no="33333")])

    process_pdf_files(str(pdf_dir), str(tmp_path / "results"), ["a.pdf", "b.pdf"])

    names = sorted(os.listdir(pdf_dir))
    assert names == ["33333.pdf", "33333_1.pdf"]


def test_page_without_text_is_reported_not_renamed(tmp_path, no_ocr_text):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    make_pdf(pdf_dir / "blank.pdf", [[]])  # one empty page, no text layer
    results = tmp_path / "results"

    excel = process_pdf_files(str(pdf_dir), str(results), ["blank.pdf"])

    df = pd.read_excel(excel, dtype=str)
    assert df.loc[0, "Filename"] == "blank.pdf"
    assert df.loc[0, "Errors"] == "No text extracted"
    assert (pdf_dir / "blank.pdf").exists()


def test_scanned_pdf_with_ocr_turned_off_raises_clear_error(tmp_path, monkeypatch):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    make_pdf(pdf_dir / "scan.pdf", [[]])
    monkeypatch.setattr(processor, "OCR_ENGINE", "none")

    with pytest.raises(OCRUnavailableError):
        process_pdf_files(str(pdf_dir), str(tmp_path / "results"), ["scan.pdf"])


def test_tesseract_missing_raises_clear_error(tmp_path, monkeypatch):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    make_pdf(pdf_dir / "scan.pdf", [[]])
    monkeypatch.setattr(processor, "OCR_ENGINE", "tesseract")
    monkeypatch.setattr(processor.shutil, "which", lambda _name: None)

    with pytest.raises(OCRUnavailableError):
        process_pdf_files(str(pdf_dir), str(tmp_path / "results"), ["scan.pdf"])


def test_easyocr_missing_raises_clear_error(tmp_path, monkeypatch):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    make_pdf(pdf_dir / "scan.pdf", [[]])
    monkeypatch.setattr(processor, "OCR_ENGINE", "easyocr")
    # Simulate "easyocr not installed" even if it happens to be present.
    monkeypatch.setitem(sys.modules, "easyocr", None)
    monkeypatch.setattr(processor, "_READER", None)

    with pytest.raises(OCRUnavailableError):
        process_pdf_files(str(pdf_dir), str(tmp_path / "results"), ["scan.pdf"])


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_tesseract_reads_scanned_page_and_renames(tmp_path):
    """Build an image-only PDF (no text layer), then check OCR finds the Staff No."""
    text_pdf = make_pdf(tmp_path / "text.pdf", [gatepass_lines(staff_no="45678")])
    src = pymupdf.open(str(text_pdf))
    png = src[0].get_pixmap(dpi=200).tobytes("png")
    src.close()

    scan_doc = pymupdf.open()
    page = scan_doc.new_page()
    page.insert_image(page.rect, stream=png)
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    scan_doc.save(str(pdf_dir / "scan.pdf"))
    scan_doc.close()

    excel = process_pdf_files(str(pdf_dir), str(tmp_path / "results"), ["scan.pdf"])

    df = pd.read_excel(excel, dtype=str)
    assert df.loc[0, "Errors"] != "No text extracted"
    assert df.loc[0, "Staff No."] == "45678"
    assert (pdf_dir / "45678.pdf").exists()


def test_staff_filename_stem_keeps_only_safe_characters():
    assert safe_staff_filename_stem("12/3\\4..5") == "12345"
    assert safe_staff_filename_stem("A-12_b") == "A-12_b"
    assert safe_staff_filename_stem("") == ""
    assert safe_staff_filename_stem("...") == ""


def test_no_hardcoded_drive_paths_in_source():
    # A drive path inside a string literal, e.g. "Y:\soma" or 'E:/data'.
    pattern = re.compile(r"""["'][A-Za-z]:[\\/]""")
    for name in ("processor.py", "app.py"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            source = handle.read()
        assert not pattern.search(source), f"{name} contains a hardcoded drive path"
