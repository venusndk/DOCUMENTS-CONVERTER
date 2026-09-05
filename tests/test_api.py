"""
Tests for the Phase 3 minimal API (documents_converter/api/app.py).

Uses the same synthetic, fabricated fixture as the pipeline tests -- see
tests/fixtures/synthetic_scan.py and docs/PHASE_0_AUDIT.md risk register
item #1 for why real scanned documents are never used here.
"""

import io
import zipfile

import openpyxl
from fastapi.testclient import TestClient

from documents_converter.api import config
from documents_converter.api.app import app

from conftest import requires_tesseract

client = TestClient(app)


def test_health_reports_status_and_tesseract_availability():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["tesseract_available"], bool)


def test_convert_rejects_unsupported_extension():
    resp = client.post(
        "/api/v1/convert",
        files={"file": ("not_a_document.exe", io.BytesIO(b"whatever"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["detail"]


def test_convert_rejects_oversized_upload(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)  # anything at all is "too big"
    resp = client.post(
        "/api/v1/convert",
        files={"file": ("scan.pdf", io.BytesIO(b"%PDF-1.4 fake content"), "application/pdf")},
    )
    assert resp.status_code == 413


@requires_tesseract
def test_convert_end_to_end_returns_valid_xlsx(synthetic_pdf, tesseract_cmd, monkeypatch):
    """
    Full round trip through the actual HTTP layer: upload the synthetic
    fixture, get back a real, correctly-populated .xlsx -- not just a
    non-error status code. Mirrors the exact expected values already
    locked in by test_ocr_excel.py's end-to-end pipeline test.
    """
    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    with open(synthetic_pdf, "rb") as f:
        resp = client.post(
            "/api/v1/convert",
            files={"file": ("synthetic_scan.pdf", f, "application/pdf")},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>" not in xml  # formula-injection regression guard, same as the pipeline test

    wb = openpyxl.load_workbook(io.BytesIO(resp.content))
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[1][:5] == ("1", "100000001", "SMITH", "JOHN", "M")
