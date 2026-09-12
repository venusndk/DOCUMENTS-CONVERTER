"""
Tests for the Phase 15 (master directive numbering: Document Preview &
Human Review) endpoints: POST /api/v1/preview and
GET /api/v1/jobs/{id}/preview/pages/{page_number}.

The job-based endpoint needs a real completed job, so those tests are
marked @requires_redis (same shape as tests/test_job_queue_api.py) and
use target="text" against a real, digital-text PDF -- fast and
deterministic, no Tesseract/OCR needed on top of the Redis this file's
job-based tests already require. POST /api/v1/preview itself needs no
job/queue at all, so its own tests run everywhere this project's suite
does.
"""

from __future__ import annotations

import base64
import io
import time

import fitz
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from documents_converter.api.app import _rate_limiter, app

from conftest import requires_redis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _build_pdf_bytes(page_texts: list[str], width=400, height=150) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page(width=width, height=height)
        page.insert_text((30, 75), text)
    data = doc.tobytes()
    doc.close()
    return data


def _wait_for_status(job_id: str, target_statuses: set[str], timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/jobs/{job_id}")
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in target_statuses:
            return status
        time.sleep(0.1)
    return status


# --------------------------------------------------------------------------
# POST /api/v1/preview
# --------------------------------------------------------------------------


def test_preview_returns_image_and_native_text_for_a_digital_pdf():
    data = _build_pdf_bytes(["First Page Text", "Second Page Text"])
    resp = client.post(
        "/api/v1/preview", data={"page": "2"}, files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 2
    assert body["page_count"] == 2
    assert body["is_ocr"] is False
    assert "Second Page Text" in body["text_preview"]

    image_bytes = base64.b64decode(body["image_base64"])
    img = Image.open(io.BytesIO(image_bytes))
    assert img.format == "PNG"


def test_preview_defaults_to_page_1():
    data = _build_pdf_bytes(["Only Text Here"])
    resp = client.post("/api/v1/preview", files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert "Only Text Here" in body["text_preview"]


def test_preview_rejects_an_out_of_range_page():
    data = _build_pdf_bytes(["Only Page"])
    resp = client.post(
        "/api/v1/preview", data={"page": "5"}, files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")}
    )
    assert resp.status_code == 400


def test_preview_rejects_an_unsupported_extension():
    resp = client.post(
        "/api/v1/preview", files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}
    )
    assert resp.status_code == 400


def test_preview_requires_auth_when_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    data = _build_pdf_bytes(["Text"])
    resp = client.post("/api/v1/preview", files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")})
    assert resp.status_code == 401


def test_preview_works_for_a_plain_image_upload():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), color=(200, 60, 60)).save(buf, "PNG")
    resp = client.post(
        "/api/v1/preview", files={"file": ("photo.png", io.BytesIO(buf.getvalue()), "image/png")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_count"] == 1
    image_bytes = base64.b64decode(body["image_base64"])
    img = Image.open(io.BytesIO(image_bytes))
    assert img.size == (300, 200)


# --------------------------------------------------------------------------
# GET /api/v1/jobs/{id}/preview/pages/{page_number}
# --------------------------------------------------------------------------


@requires_redis
def test_job_page_preview_returns_the_real_source_page():
    data = _build_pdf_bytes(["Job Page One", "Job Page Two"])
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _wait_for_status(job_id, {"completed", "failed"}) == "completed"

    preview_resp = client.get(f"/api/v1/jobs/{job_id}/preview/pages/2")
    assert preview_resp.status_code == 200
    assert preview_resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(preview_resp.content))
    assert img.format == "PNG"


@requires_redis
def test_job_page_preview_rejects_an_out_of_range_page():
    data = _build_pdf_bytes(["Only Page"])
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    _wait_for_status(job_id, {"completed", "failed"})

    preview_resp = client.get(f"/api/v1/jobs/{job_id}/preview/pages/9")
    assert preview_resp.status_code == 400


def test_job_page_preview_404_for_unknown_job():
    resp = client.get("/api/v1/jobs/does-not-exist/preview/pages/1")
    assert resp.status_code == 404


def test_job_page_preview_requires_auth_when_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    resp = client.get("/api/v1/jobs/does-not-exist/preview/pages/1")
    assert resp.status_code == 401
