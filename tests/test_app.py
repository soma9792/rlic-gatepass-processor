import io
import logging
import os
import tempfile
import zipfile

import pandas as pd
import pytest

import app as app_module
import processor
from processor import OCRUnavailableError
from tests.conftest import gatepass_lines, make_pdf


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client. Temp folders go to tmp_path so leftovers are visible."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


def pdf_bytes(tmp_path, pages):
    path = make_pdf(tmp_path / "source.pdf", pages)
    return path.read_bytes()


def upload(name, data):
    return (io.BytesIO(data), name)


def leftover_work_dirs(tmp_path):
    return [p for p in (tmp_path / "tmp").iterdir() if p.name.startswith("rlic_")]


def test_index_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Upload PDF Files" in response.data
    assert b'action="/process"' in response.data


def test_valid_upload_returns_zip_with_renamed_pdfs_and_excel(client, tmp_path):
    data = pdf_bytes(tmp_path, [gatepass_lines(staff_no="55555")])

    response = client.post(
        "/process",
        data={"pdfs": [upload("gate pass.pdf", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"

    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        names = archive.namelist()
        assert "renamed_pdfs/55555.pdf" in names
        xlsx_names = [n for n in names if n.endswith(".xlsx")]
        assert len(xlsx_names) == 1
        excel = pd.read_excel(io.BytesIO(archive.read(xlsx_names[0])), dtype=str)
        assert excel.loc[0, "Staff No."] == "55555"
        assert excel.loc[0, "Filename"] == "55555.pdf"


def test_no_work_folder_is_left_after_request(client, tmp_path):
    data = pdf_bytes(tmp_path, [gatepass_lines(staff_no="66666")])

    response = client.post(
        "/process",
        data={"pdfs": [upload("a.pdf", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert leftover_work_dirs(tmp_path) == []


def test_non_pdf_extension_is_rejected(client, tmp_path):
    data = pdf_bytes(tmp_path, [gatepass_lines()])

    response = client.post(
        "/process",
        data={"pdfs": [upload("notes.txt", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"Only PDF files are accepted." in response.data


def test_fake_pdf_with_pdf_extension_is_rejected_by_header(client):
    response = client.post(
        "/process",
        data={"pdfs": [upload("fake.pdf", b"this is not a pdf at all")]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"not a valid PDF" in response.data


def test_corrupt_pdf_with_valid_header_is_rejected(client):
    response = client.post(
        "/process",
        data={"pdfs": [upload("broken.pdf", b"%PDF-1.4\nthis is cut off")]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"not a valid PDF" in response.data


def test_no_files_selected_is_rejected(client):
    response = client.post("/process", data={}, content_type="multipart/form-data")

    assert response.status_code == 400
    assert b"Please select at least one PDF file." in response.data


def test_too_many_files_is_rejected(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILES", 2)
    data = pdf_bytes(tmp_path, [gatepass_lines()])

    response = client.post(
        "/process",
        data={"pdfs": [upload(f"f{i}.pdf", data) for i in range(3)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"Too many files" in response.data


def test_file_over_size_limit_is_rejected(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILE_MB", 0)  # any real PDF is over 0 bytes
    data = pdf_bytes(tmp_path, [gatepass_lines()])

    response = client.post(
        "/process",
        data={"pdfs": [upload("big.pdf", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"larger than" in response.data


def test_total_upload_size_over_limit_returns_413(client, tmp_path):
    data = pdf_bytes(tmp_path, [gatepass_lines()])
    app_module.app.config["MAX_CONTENT_LENGTH"] = 200  # bytes; the PDF is bigger
    try:
        response = client.post(
            "/process",
            data={"pdfs": [upload("a.pdf", data)]},
            content_type="multipart/form-data",
        )
    finally:
        app_module.app.config["MAX_CONTENT_LENGTH"] = app_module.MAX_TOTAL_MB * 1024 * 1024

    assert response.status_code == 413
    assert b"larger than" in response.data


def test_page_limit_is_enforced(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_TOTAL_PAGES", 1)
    data = pdf_bytes(tmp_path, [gatepass_lines(), gatepass_lines()])

    response = client.post(
        "/process",
        data={"pdfs": [upload("two_pages.pdf", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"Too many pages" in response.data


def test_password_protected_pdf_is_rejected(client, tmp_path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret", fontsize=11)
    encrypted = tmp_path / "locked.pdf"
    doc.save(str(encrypted), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()

    response = client.post(
        "/process",
        data={"pdfs": [upload("locked.pdf", encrypted.read_bytes())]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"Password-protected" in response.data


def test_unsafe_filename_is_sanitised(client, tmp_path):
    data = pdf_bytes(tmp_path, [gatepass_lines(staff_no="77777")])

    response = client.post(
        "/process",
        data={"pdfs": [upload("../../etc/evil name.pdf", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        for name in archive.namelist():
            assert ".." not in name
            assert not name.startswith("/")


def test_scanned_pdf_without_ocr_returns_503(client, tmp_path, monkeypatch):
    data = pdf_bytes(tmp_path, [[]])  # blank page, no text layer
    monkeypatch.setattr(processor, "OCR_ENGINE", "none")

    response = client.post(
        "/process",
        data={"pdfs": [upload("scan.pdf", data)]},
        content_type="multipart/form-data",
    )

    assert response.status_code == 503
    assert leftover_work_dirs(tmp_path) == []


def test_logs_never_contain_document_text(client, tmp_path, caplog):
    marker = "ZXQ-SECRET-MARKER-4242"
    data = pdf_bytes(tmp_path, [gatepass_lines(name=marker, staff_no="88888")])

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/process",
            data={"pdfs": [upload("private name.pdf", data)]},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in logged
    assert "private name" not in logged


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_security_headers_present(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
