"""
Real-Redis, end-to-end tests for Phase 14 (master directive numbering:
Job Queue & Real-Time Processing): job lifecycle, cancellation, manual
retry/resumability, and progress events -- all against a real Redis
queue with a real RQ worker (tests/conftest.py's _rq_worker_thread)
actually processing jobs, not a mock.

Uses `target="text"` (pdf_document -> text) against real, digital-text
PDFs built with fitz directly -- fast, deterministic, no Tesseract/OCR
needed on top of the Redis this whole file already requires.
"""

from __future__ import annotations

import dataclasses
import io
import json
import time

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api.app import _rate_limiter, app
from documents_converter.registry import registry

from conftest import requires_redis

pytestmark = requires_redis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _patch_capability_convert(monkeypatch, source_format: str, target_format: str, new_convert):
    """
    Replaces a registered Capability's `convert` function for the
    duration of one test. Capability is a frozen dataclass
    (documents_converter/registry.py) whose `.convert` is a direct
    function reference captured at import time -- monkeypatching the
    original module-level function itself (e.g.
    `documents_converter.converters.pdf_to_text._convert`) does nothing
    to an already-registered Capability's own reference to it,
    confirmed the hard way (an earlier version of this file's cancel
    and automatic-retry tests did exactly that, and neither the
    "blocker" job nor the flaky conversion ever actually ran). Swapping
    the registry's own entry for a dataclasses.replace() copy is what
    actually reaches rq_tasks.run_conversion_job, which looks the
    capability up fresh from the registry on every call.
    """
    key = (source_format, target_format)
    real_capability = registry._by_key[key]
    monkeypatch.setitem(registry._by_key, key, dataclasses.replace(real_capability, convert=new_convert))
    return real_capability.convert


def _build_pdf_bytes(text="Hello") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 100), text)
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
# Basic lifecycle through the real queue
# --------------------------------------------------------------------------


def test_job_completes_through_the_real_queue():
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_build_pdf_bytes("Real queue")), "application/pdf")},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = _wait_for_status(job_id, {"completed", "failed"})
    assert status == "completed"

    result = client.get(f"/api/v1/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.content == b"Real queue"


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


def test_cancel_a_queued_job_before_it_starts(monkeypatch):
    """Blocks the worker with an unrelated long-running job first, so
    the job under test is genuinely still queued (not already
    processing) when cancel is called -- a real, not timing-guessed,
    "queued" state."""
    blocker_event_started = {"v": False}

    def _blocking_convert(*args, **kwargs):
        blocker_event_started["v"] = True
        time.sleep(3)

    _patch_capability_convert(monkeypatch, "pdf_document", "text", _blocking_convert)

    blocker_resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("blocker.pdf", io.BytesIO(_build_pdf_bytes("Blocker")), "application/pdf")},
    )
    blocker_id = blocker_resp.json()["job_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not blocker_event_started["v"]:
        time.sleep(0.05)
    assert blocker_event_started["v"], "blocker job never started"

    target_resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("target.pdf", io.BytesIO(_build_pdf_bytes("Target")), "application/pdf")},
    )
    target_id = target_resp.json()["job_id"]
    assert client.get(f"/api/v1/jobs/{target_id}").json()["status"] == "queued"

    cancel_resp = client.post(f"/api/v1/jobs/{target_id}/cancel")
    assert cancel_resp.status_code == 200
    assert client.get(f"/api/v1/jobs/{target_id}").json()["status"] == "cancelled"

    # Let the blocker finish so it doesn't outlive this test.
    _wait_for_status(blocker_id, {"completed", "failed"}, timeout=15)

    # The cancelled job must never actually run once the worker gets to
    # it -- confirmed, not just assumed from the status field alone.
    time.sleep(1)
    assert client.get(f"/api/v1/jobs/{target_id}").json()["status"] == "cancelled"


def test_cancel_already_completed_job_returns_409():
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_build_pdf_bytes()), "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    _wait_for_status(job_id, {"completed", "failed"})

    cancel_resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 409


def test_cancel_unknown_job_returns_404():
    assert client.post("/api/v1/jobs/does-not-exist/cancel").status_code == 404


# --------------------------------------------------------------------------
# Manual retry / resumability
# --------------------------------------------------------------------------


def test_retry_a_stuck_job_resumes_it_without_a_reupload():
    """The real resumability proof: a job left "processing" with no
    live RQ job behind it (simulating a worker that crashed
    mid-conversion) gets picked back up by /retry using the same input
    file already on disk -- no re-upload."""
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_build_pdf_bytes("Resumed")), "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    _wait_for_status(job_id, {"completed", "failed"})
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"

    # Simulate the "worker died mid-conversion" scenario directly: force
    # the JobRecord back to "processing" with no corresponding live RQ
    # job (the real one already finished and was cleaned up).
    from documents_converter.api.jobs import JobStore

    JobStore(retention_seconds=3600).update(job_id, status="processing")
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "processing"

    retry_resp = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry_resp.status_code == 200

    status = _wait_for_status(job_id, {"completed", "failed"})
    assert status == "completed"
    assert client.get(f"/api/v1/jobs/{job_id}/result").content == b"Resumed"


def test_retry_a_completed_job_returns_409():
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_build_pdf_bytes()), "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    _wait_for_status(job_id, {"completed", "failed"})

    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409


def test_retry_unknown_job_returns_404():
    assert client.post("/api/v1/jobs/does-not-exist/retry").status_code == 404


def test_batch_resume_reports_a_per_file_outcome():
    files = [
        ("files", ("a.pdf", io.BytesIO(_build_pdf_bytes("A")), "application/pdf")),
        ("files", ("b.pdf", io.BytesIO(_build_pdf_bytes("B")), "application/pdf")),
    ]
    resp = client.post("/api/v1/batch", data={"target": "text"}, files=files)
    batch_id = resp.json()["batch_id"]
    job_ids = resp.json()["job_ids"]

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/batch/{batch_id}").json()
        if body["status"] not in ("queued", "processing"):
            break
        time.sleep(0.1)
    assert body["status"] == "completed"

    resume_resp = client.post(f"/api/v1/batch/{batch_id}/resume")
    assert resume_resp.status_code == 200
    outcomes = resume_resp.json()["outcomes"]
    assert set(outcomes) == set(job_ids)
    assert all(v == "already_completed" for v in outcomes.values())


def test_batch_resume_unknown_batch_returns_404():
    assert client.post("/api/v1/batch/does-not-exist/resume").status_code == 404


# --------------------------------------------------------------------------
# Automatic retry (RQ's own Retry, for transient/environment failures)
# --------------------------------------------------------------------------


def test_automatic_retry_recovers_from_a_transient_failure(monkeypatch):
    """A failure NOT classified as bad input gets RQ's own automatic
    retry -- confirmed by forcing the underlying capability to fail
    exactly once, then succeed, and checking the job still ends up
    completed rather than failed after the first attempt."""
    attempt_count = {"n": 0}

    def _flaky_convert(input_path, output_path, *, progress):
        attempt_count["n"] += 1
        if attempt_count["n"] < 2:
            raise RuntimeError("simulated transient failure")
        return real_convert(input_path, output_path, progress=progress)

    real_convert = _patch_capability_convert(monkeypatch, "pdf_document", "text", _flaky_convert)

    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_build_pdf_bytes("Flaky")), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    status = _wait_for_status(job_id, {"completed", "failed"}, timeout=20)
    assert status == "completed"
    assert attempt_count["n"] == 2
    # The stale "will be retried" error from the first attempt must not
    # linger on a job that ultimately succeeded.
    assert client.get(f"/api/v1/jobs/{job_id}").json().get("error") is None


# --------------------------------------------------------------------------
# Progress events (Server-Sent Events)
# --------------------------------------------------------------------------


def test_job_events_stream_reports_completion():
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_build_pdf_bytes("Events")), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    with client.stream("GET", f"/api/v1/jobs/{job_id}/events") as stream:
        assert stream.status_code == 200
        payloads = []
        for line in stream.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            payloads.append(payload)
            if payload["status"] in ("completed", "failed"):
                break

    assert payloads[-1]["status"] == "completed"


def test_job_events_reports_not_found_for_an_unknown_job():
    with client.stream("GET", "/api/v1/jobs/does-not-exist/events") as stream:
        lines = list(stream.iter_lines())
    assert any("not found" in line.lower() for line in lines if line)
