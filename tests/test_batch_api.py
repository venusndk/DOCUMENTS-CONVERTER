"""
Tests for the /api/v1/batch endpoints (documents_converter/api/app.py,
batch.py) -- Phase 13, master directive numbering (Batch Processing).

Uses `target="text"` (pdf_document -> text, documents_converter/
converters/pdf_to_text.py) against real, digital-text PDFs built with
fitz directly -- fast and deterministic, no Tesseract/OCR involved.

Every test that actually waits for a queued job/batch to complete is
marked @requires_redis (Phase 14, master directive numbering: these
now go through a real Redis/RQ queue, not an in-process thread pool --
see tests/conftest.py's @requires_redis and documents_converter/api/
job_queue.py). Same shape as the pre-existing @requires_tesseract/
@requires_libreoffice gaps: not installed on this project's own dev
machine, real verification happens in Docker Compose and CI.
"""

from __future__ import annotations

import io
import time
import zipfile

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api.app import _rate_limiter, app

from conftest import requires_redis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _build_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 100), text)
    data = doc.tobytes()
    doc.close()
    return data


def _wait_for_batch_terminal(batch_id: str, timeout: float = 30) -> dict:
    """Polls a batch until its overall status leaves queued/processing
    or `timeout` elapses, returning the last observed status body --
    same convention as test_api.py's _wait_for_job_terminal."""
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/batch/{batch_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] not in ("queued", "processing"):
            return body
        time.sleep(0.2)
    return body


@requires_redis
def test_create_batch_queues_every_file_and_all_complete():
    files = [
        ("files", ("a.pdf", io.BytesIO(_build_pdf_bytes("Alpha")), "application/pdf")),
        ("files", ("b.pdf", io.BytesIO(_build_pdf_bytes("Beta")), "application/pdf")),
        ("files", ("c.pdf", io.BytesIO(_build_pdf_bytes("Gamma")), "application/pdf")),
    ]
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=files)
    assert resp.status_code == 202
    body = resp.json()
    assert body["total"] == 3
    assert len(body["job_ids"]) == 3

    status = _wait_for_batch_terminal(body["batch_id"])
    assert status["status"] == "completed"
    assert status["counts"] == {"completed": 3}
    assert len(status["files"]) == 3
    assert all(f["status"] == "completed" for f in status["files"])


def test_create_batch_requires_at_least_one_file():
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=[])
    assert resp.status_code in (400, 422)


@requires_redis
def test_batch_reports_individual_failures_without_blocking_the_rest():
    """One file with an extension that can't satisfy `target` must be
    recorded as its own failed job, not abort the whole batch -- the
    other, valid file still gets queued and completes."""
    files = [
        ("files", ("good.pdf", io.BytesIO(_build_pdf_bytes("Valid")), "application/pdf")),
        ("files", ("bad.txt", io.BytesIO(b"not a pdf"), "text/plain")),
    ]
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=files)
    assert resp.status_code == 202
    batch_id = resp.json()["batch_id"]

    status = _wait_for_batch_terminal(batch_id)
    assert status["status"] == "completed_with_errors"
    assert status["counts"] == {"completed": 1, "failed": 1}
    failed_entries = [f for f in status["files"] if f["status"] == "failed"]
    assert len(failed_entries) == 1
    assert "error" in failed_entries[0]


@requires_redis
def test_batch_download_bundles_results_and_a_manifest():
    files = [
        ("files", ("good.pdf", io.BytesIO(_build_pdf_bytes("Valid")), "application/pdf")),
        ("files", ("bad.txt", io.BytesIO(b"not a pdf"), "text/plain")),
    ]
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=files)
    batch_id = resp.json()["batch_id"]
    _wait_for_batch_terminal(batch_id)

    download_resp = client.get(f"/api/v1/batch/{batch_id}/download")
    assert download_resp.status_code == 200
    assert download_resp.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(download_resp.content)) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        # One successful result (named by job id + its real extension),
        # not one entry per uploaded file -- the failed file produced no
        # job output to bundle, only a manifest entry.
        result_names = names - {"manifest.json"}
        assert len(result_names) == 1
        assert next(iter(result_names)).endswith(".txt")

        import json

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["batch_id"] == batch_id
        assert len(manifest["files"]) == 2


@requires_redis
def test_batch_download_409_before_finished(monkeypatch):
    def _slow_convert(*args, **kwargs):
        time.sleep(5)

    monkeypatch.setattr("documents_converter.converters.pdf_to_text._convert", _slow_convert)

    files = [("files", ("a.pdf", io.BytesIO(_build_pdf_bytes("Alpha")), "application/pdf"))]
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=files)
    batch_id = resp.json()["batch_id"]

    download_resp = client.get(f"/api/v1/batch/{batch_id}/download")
    assert download_resp.status_code == 409

    # Same hygiene as test_api.py's equivalent job-level test: don't let
    # the monkeypatched slow conversion outlive this test.
    _wait_for_batch_terminal(batch_id, timeout=15)


def test_get_batch_status_404_for_unknown_id():
    resp = client.get("/api/v1/batch/does-not-exist")
    assert resp.status_code == 404


def test_get_batch_download_404_for_unknown_id():
    resp = client.get("/api/v1/batch/does-not-exist/download")
    assert resp.status_code == 404


def test_batch_endpoint_requires_auth_when_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    files = [("files", ("a.pdf", io.BytesIO(_build_pdf_bytes("Alpha")), "application/pdf"))]
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=files)
    assert resp.status_code == 401
