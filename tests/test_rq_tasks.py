"""
Tests for documents_converter/api/rq_tasks.py's run_conversion_job that
don't need a real Redis -- called directly, the same way an RQ worker
would eventually call it, but outside any actual RQ job context
(rq.get_current_job() returns None here, same as it would for a plain
unit test), which is exactly what exercises this function's two
disclosed constraints for real: rq_tasks.py must never import app.py
(these tests would themselves prove a circular import by failing to
collect at all if it did), and _report_progress/the retry-branch must
both degrade gracefully with no RQ job in context.

Phase 14, master directive numbering (Job Queue & Real-Time
Processing). Real-Redis end-to-end lifecycle tests (retry, cancel,
resumability, progress events) are in tests/test_job_queue_api.py,
marked @requires_redis.
"""

from __future__ import annotations

import fitz
import pytest

from documents_converter.api import security
from documents_converter.api.jobs import JobStore
from documents_converter.api.rq_tasks import run_conversion_job


def _build_pdf(path, text="Hello") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 100), text)
    doc.save(str(path))
    doc.close()


def test_run_conversion_job_completes_successfully(tmp_path):
    store = JobStore(retention_seconds=3600)
    job = store.create(target="text")
    input_path = tmp_path / "input.pdf"
    _build_pdf(input_path, "Hello RQ")

    run_conversion_job(job.id, str(input_path), ".pdf", "text", "req1")

    fetched = store.get(job.id)
    assert fetched.status == "completed"
    assert fetched.error is None
    assert fetched.result_path.read_text() == "Hello RQ"


def test_run_conversion_job_fails_cleanly_for_an_unsupported_target(tmp_path):
    store = JobStore(retention_seconds=3600)
    job = store.create(target="no-such-target")
    input_path = tmp_path / "input.pdf"
    _build_pdf(input_path)

    # Must not raise -- a bad-input failure is final and reported via
    # the JobRecord, not propagated (there is nothing to retry it into
    # succeeding, and outside a real RQ job context there is no
    # retry/failure machinery to catch a raised exception anyway).
    run_conversion_job(job.id, str(input_path), ".pdf", "no-such-target", "req2")

    fetched = store.get(job.id)
    assert fetched.status == "failed"
    assert "Unsupported file type" in fetched.error


def test_run_conversion_job_fails_cleanly_on_a_decompression_bomb(tmp_path, monkeypatch):
    store = JobStore(retention_seconds=3600)
    job = store.create(target="text")
    input_path = tmp_path / "input.pdf"
    _build_pdf(input_path)

    monkeypatch.setattr(security, "MAX_PDF_PAGES", 0)

    run_conversion_job(job.id, str(input_path), ".pdf", "text", "req3")

    fetched = store.get(job.id)
    assert fetched.status == "failed"
    assert "page limit" in fetched.error


def test_run_conversion_job_reraises_an_unexpected_error_outside_a_worker_context(tmp_path):
    """No RQ job in context (get_current_job() is None here) means
    there is no retry machinery watching for this exception -- it must
    still propagate rather than being silently swallowed, so a real RQ
    worker (which DOES have a job in context) is the only place this
    exception is ever caught and turned into a retry decision."""
    store = JobStore(retention_seconds=3600)
    job = store.create(target="text")
    # A path that doesn't exist makes capability.convert's own file I/O
    # raise something that isn't ValueError/FileTooLargeError -- an
    # "unexpected" failure by this function's own classification.
    missing_path = tmp_path / "does-not-exist.pdf"

    with pytest.raises(Exception):
        run_conversion_job(job.id, str(missing_path), ".pdf", "text", "req4")

    fetched = store.get(job.id)
    assert fetched.status == "failed"
    assert fetched.error == "Conversion failed. This has been logged for investigation."
