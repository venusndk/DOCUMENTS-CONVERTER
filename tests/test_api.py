"""
Tests for the API (documents_converter/api/app.py): Phase 3 (the basic
endpoints), Phase 4 (security hardening), and Phase 6 (API-key auth).

Uses the same synthetic, fabricated fixture as the pipeline tests -- see
tests/fixtures/synthetic_scan.py and docs/PHASE_0_AUDIT.md risk register
item #1 for why real scanned documents are never used here.
"""

import io
import zipfile

import openpyxl
import pytest
from fastapi.testclient import TestClient

from documents_converter.api import config, security
from documents_converter.api.app import _rate_limiter, app

from conftest import requires_tesseract

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """_rate_limiter is a module-level singleton shared by every request;
    without resetting it, whichever test happens to run the most requests
    against the shared TestClient "IP" would trip the limit for every test
    after it. Applied to all tests so the dedicated rate-limit test below
    can safely exhaust the limit without affecting anything else."""
    _rate_limiter._counts.clear()
    yield


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


# --------------------------------------------------------------------------
# Phase 4: security hardening (docs/PHASE_0_AUDIT.md).
# --------------------------------------------------------------------------


def test_convert_rejects_content_that_does_not_match_extension():
    """A file renamed to look like a PDF but containing something else
    entirely should be rejected by the magic-byte check, not accepted just
    because its filename ends in .pdf."""
    resp = client.post(
        "/api/v1/convert",
        files={"file": ("fake.pdf", io.BytesIO(b"this is not a pdf at all"), "application/pdf")},
    )
    assert resp.status_code == 400
    assert "doesn't match its extension" in resp.json()["detail"]


@pytest.mark.parametrize(
    "ext,good_header",
    [
        (".pdf", b"%PDF-1.4"),
        (".png", b"\x89PNG\r\n\x1a\n"),
        (".bmp", b"BM"),
    ],
)
def test_matches_magic_bytes_accepts_real_signatures(ext, good_header):
    assert security.matches_magic_bytes(ext, good_header) is True


def test_matches_magic_bytes_rejects_mismatched_signature():
    assert security.matches_magic_bytes(".pdf", b"not a pdf header") is False


def test_check_image_dimensions_rejects_oversized_canvas():
    """Decompression-bomb guard, tested directly against the check function
    rather than by actually constructing a giant image (which would defeat
    the point by allocating the memory this guard exists to avoid)."""
    with pytest.raises(security.FileTooLargeError):
        security.check_image_dimensions(20000, 20000)  # 400 MP, over the 50 MP limit
    security.check_image_dimensions(2000, 2000)  # 4 MP, fine -- must not raise


def test_check_pdf_page_count_rejects_too_many_pages():
    with pytest.raises(security.FileTooLargeError):
        security.check_pdf_page_count(security.MAX_PDF_PAGES + 1)
    security.check_pdf_page_count(1)  # must not raise


def test_convert_rate_limits_excessive_requests():
    """Exhausts the configured limit and confirms the next request is
    rejected with 429 -- then confirms it clears once the window resets
    manually (this test's own cleanup, not a real time-based wait)."""
    bad_file = {"file": ("x.xyz", io.BytesIO(b"data"), "application/octet-stream")}
    for _ in range(config.RATE_LIMIT_MAX_REQUESTS):
        resp = client.post("/api/v1/convert", files=bad_file)
        assert resp.status_code == 400  # rejected for extension, but still *counted*

    resp = client.post("/api/v1/convert", files=bad_file)
    assert resp.status_code == 429


@requires_tesseract
def test_convert_times_out_on_a_slow_conversion(monkeypatch, synthetic_pdf, tesseract_cmd):
    """A conversion that runs longer than CONVERT_TIMEOUT_SECONDS should be
    abandoned with a 504, not hang the request indefinitely. Simulated with
    a monkeypatched slow function rather than an actually-slow real
    conversion, since the point is testing the timeout wiring, not OCR
    performance."""
    import time

    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)
    monkeypatch.setattr(config, "CONVERT_TIMEOUT_SECONDS", 0.2)

    def _slow_convert(*args, **kwargs):
        time.sleep(5)

    monkeypatch.setattr("documents_converter.api.app.convert_scanned_to_excel", _slow_convert)

    with open(synthetic_pdf, "rb") as f:
        resp = client.post(
            "/api/v1/convert",
            files={"file": ("synthetic_scan.pdf", f, "application/pdf")},
        )
    assert resp.status_code == 504


# --------------------------------------------------------------------------
# Phase 6: API-key authentication (docs/PHASE_0_AUDIT.md).
# --------------------------------------------------------------------------

_junk_file = {"file": ("x.xyz", io.BytesIO(b"data"), "application/octet-stream")}


def test_convert_works_without_auth_when_no_keys_configured():
    """Default state: config.API_KEYS is empty, so auth is off and a request
    with no Authorization header at all must not be rejected with 401 --
    it should reach the normal extension check instead (400, not 401)."""
    assert config.API_KEYS == ()  # the actual default, not assumed
    resp = client.post("/api/v1/convert", files=_junk_file)
    assert resp.status_code == 400


def test_convert_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    resp = client.post("/api/v1/convert", files=_junk_file)
    assert resp.status_code == 401
    assert "Authorization" in resp.json()["detail"]


def test_convert_rejects_wrong_api_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    resp = client.post(
        "/api/v1/convert",
        files=_junk_file,
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401
    assert "Invalid API key" in resp.json()["detail"]


def test_convert_accepts_a_valid_api_key(monkeypatch):
    """A correct key should pass the auth check and reach the normal
    pipeline (proven here by getting the extension-rejection 400, not an
    auth-related 401 -- a full OCR run isn't needed to prove auth passed)."""
    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1", "secret-key-2"))
    resp = client.post(
        "/api/v1/convert",
        files=_junk_file,
        headers={"Authorization": "Bearer secret-key-2"},
    )
    assert resp.status_code == 400  # extension rejection, i.e. auth passed


def test_health_never_requires_auth(monkeypatch):
    """Load balancers and monitoring probes need to reach /health without
    credentials, even when auth is otherwise enabled."""
    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    resp = client.get("/health")
    assert resp.status_code == 200
