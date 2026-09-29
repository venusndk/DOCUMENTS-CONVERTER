"""
Timeout testing and failure injection (Phase 19, master directive
numbering: Performance & Security Hardening).

Two real bugs this pass found and fixed, both in the async job-creation
path (create_job/create_batch, app.py) -- the synchronous
/api/v1/convert path already had a broad `except Exception` + `finally:
storage.release(...)` guarding this exact step; these two never did:

1. create_job: a file that clears matches_magic_bytes but is malformed
   enough to make check_decompression_bomb's own parsing choke (a real,
   reproduced case: a corrupt-but-"%PDF"-prefixed file raising
   pymupdf.FileDataError) fell through both of the existing except
   clauses (HTTPException, security.FileTooLargeError), reaching only
   the app-wide catch-all -- which returns a clean 500, but skips the
   local cleanup those except clauses perform: storage.release(work_dir)
   never ran (a permanent disk leak for every such upload) and
   JobStore.update(..., status="failed") never ran either, leaving the
   job stuck reporting "queued" forever to a polling caller.

2. create_batch: the same gap, more severe -- left uncaught, the
   exception doesn't just fail one file's job, it breaks out of the
   whole `for file in files:` loop, silently abandoning every
   *subsequent* file in the batch too, directly contradicting this
   endpoint's own documented "one bad file costs that one file, not the
   other forty-nine."

Both reproduced against the real, unfixed code before their fixes
(broadening each except block) were written.
"""

from __future__ import annotations

import dataclasses
import io
import time

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api import config
from documents_converter.api.app import _rate_limiter, app
from documents_converter.registry import registry

from conftest import requires_redis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _corrupt_pdf_bytes() -> bytes:
    """Clears matches_magic_bytes (starts with the real %PDF signature)
    but is not a well-formed PDF -- pymupdf.fitz.open() raises a real,
    uncaught-by-name exception on this, not security.FileTooLargeError
    or anything HTTPException-shaped."""
    return b"%PDF-1.4\n" + b"this is not real PDF structure at all" * 20


def _good_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page(width=200, height=100)
    doc[0].insert_text((20, 50), text)
    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------
# create_job: corrupt file no longer leaks work_dir or sticks at "queued"
# --------------------------------------------------------------------------


def test_create_job_cleanly_fails_on_an_unexpected_validation_error():
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("corrupt.pdf", io.BytesIO(_corrupt_pdf_bytes()), "application/pdf")},
    )
    assert resp.status_code == 500
    assert "logged for investigation" in resp.json()["detail"]
    # No raw exception text/traceback in the response.
    assert "FileDataError" not in resp.text
    assert "Traceback" not in resp.text


# --------------------------------------------------------------------------
# create_batch: one corrupt file no longer aborts the whole batch
# --------------------------------------------------------------------------


def test_create_batch_isolates_one_corrupt_file_from_the_rest():
    resp = client.post(
        "/api/v1/batch",
        data={"target": "text"},
        files=[
            ("files", ("good1.pdf", io.BytesIO(_good_pdf_bytes("one")), "application/pdf")),
            ("files", ("corrupt.pdf", io.BytesIO(_corrupt_pdf_bytes()), "application/pdf")),
            ("files", ("good2.pdf", io.BytesIO(_good_pdf_bytes("two")), "application/pdf")),
        ],
    )
    # The real, meaningful assertion: this succeeds at all (202, with
    # all 3 job ids) -- before the fix, the corrupt file's exception
    # aborted the whole request with a bare 500, losing both good
    # files' submissions along with it.
    assert resp.status_code == 202
    body = resp.json()
    assert body["total"] == 3
    assert len(body["job_ids"]) == 3


@requires_redis
def test_create_batch_good_files_still_complete_despite_one_corrupt_file():
    """The full, real proof: not just that all 3 jobs were created, but
    that the two good ones actually complete through the real queue
    while the corrupt one is marked failed -- not stuck, not silently
    dropped."""
    resp = client.post(
        "/api/v1/batch",
        data={"target": "text"},
        files=[
            ("files", ("good1.pdf", io.BytesIO(_good_pdf_bytes("one")), "application/pdf")),
            ("files", ("corrupt.pdf", io.BytesIO(_corrupt_pdf_bytes()), "application/pdf")),
            ("files", ("good2.pdf", io.BytesIO(_good_pdf_bytes("two")), "application/pdf")),
        ],
    )
    job_ids = resp.json()["job_ids"]

    deadline = time.monotonic() + 15
    statuses = {}
    while time.monotonic() < deadline and len(statuses) < 3:
        for job_id in job_ids:
            if job_id in statuses:
                continue
            status = client.get(f"/api/v1/jobs/{job_id}").json()["status"]
            if status in ("completed", "failed"):
                statuses[job_id] = status
        time.sleep(0.1)

    assert len(statuses) == 3, f"not every job reached a terminal state: {statuses}"
    assert sorted(statuses.values()) == ["completed", "completed", "failed"]


# --------------------------------------------------------------------------
# JOB_MAX_RETRIES=0 -- a real bug found while writing the timeout test
# below (isolating it from RQ's separate retry behavior is what
# surfaced this)
# --------------------------------------------------------------------------


@requires_redis
def test_job_max_retries_zero_does_not_crash_job_submission(monkeypatch):
    """
    RQ's own Retry(max=0, ...) raises ValueError outright ("please
    enter a value greater than 0") rather than meaning "no retry" --
    confirmed directly against the real rq package. JOB_MAX_RETRIES=0
    is a real, documented config value (disable automatic retry
    entirely) that crashed *every* job submission with an unhandled 500
    before _enqueue_conversion_job (app.py) special-cased it to omit
    `retry=` entirely instead of constructing an invalid Retry object.

    Waits for the job to actually finish before returning -- found the
    hard way why that matters here specifically, not just as a general
    habit: an earlier version of this test returned immediately after
    the 202, and the very next test
    (test_a_hung_conversion_is_killed_by_the_configured_job_timeout)
    monkeypatches the exact same shared capability
    (registry._by_key[("pdf_document", "text")]) this job still hadn't
    been picked up to run yet. The real worker thread (shared for the
    whole test session) then executed *this* job using the *other*
    test's hung replacement function, and -- worse -- did so under
    whatever CONVERT_TIMEOUT_SECONDS was in effect at *this* job's own
    enqueue time (job_timeout is baked into the job at enqueue, not
    re-read live), a much longer default than the 1s the other test
    expected, wedging the single-threaded SimpleWorker on this job for
    that entire duration and starving the next test's own job of any
    worker attention until it finally elapsed. Confirmed by reproducing
    it standalone and dumping the stuck worker thread's live stack
    (sys._current_frames()) mid-hang: it was executing *this* test's
    job through the *other* test's hang_forever function.
    """
    monkeypatch.setattr(config, "JOB_MAX_RETRIES", 0)
    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_good_pdf_bytes("No Retries")), "application/pdf")},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + 15
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert status == "completed"


# --------------------------------------------------------------------------
# Async job timeout enforcement -- a real, previously-untested path
# --------------------------------------------------------------------------


def _patch_capability_convert(monkeypatch, source_format: str, target_format: str, new_convert):
    """See tests/test_job_queue_api.py's identical helper for why this
    (not monkeypatching the original module-level function) is what
    actually reaches rq_tasks.run_conversion_job."""
    key = (source_format, target_format)
    real_capability = registry._by_key[key]
    monkeypatch.setitem(registry._by_key, key, dataclasses.replace(real_capability, convert=new_convert))


@requires_redis
def test_a_hung_conversion_is_killed_by_the_configured_job_timeout(monkeypatch):
    """
    A capability that never returns (simulating a genuinely hung
    conversion -- an infinite loop, a wedged subprocess) must not leave
    a job stuck 'processing' forever. _enqueue_conversion_job (app.py)
    passes job_timeout=config.CONVERT_TIMEOUT_SECONDS explicitly to RQ's
    own enqueue call -- this exercises that real path end to end,
    through the real background worker (tests/conftest.py's
    _rq_worker_thread, already configured with TimerDeathPenalty -- see
    that fixture's own docstring for why that matters on Windows), not
    just reading the code and trusting the wiring is correct.

    JOB_MAX_RETRIES is monkeypatched to 0 here on purpose: RQ's own
    automatic retry (rq_tasks.run_conversion_job) treats a timeout as a
    transient failure worth retrying, same as it would a flaky I/O
    error -- confirmed for real while writing this test (the first
    version, with retries left at their configured default of 2, took
    the job through a real ~23s retry-then-backoff-then-retry cycle
    before finally landing on "failed", not a bug, just this test
    polling for a shorter window than that full cycle needs). Retry
    *exhaustion* already has its own dedicated coverage
    (test_job_queue_api.py's automatic-retry tests); this test isolates
    the one property it's actually named for -- the timeout itself
    fires and reaches a terminal state -- from that separate, already-
    covered concern.
    """
    monkeypatch.setattr(config, "CONVERT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(config, "JOB_MAX_RETRIES", 0)

    def _hang_forever(*args, **kwargs):
        while True:
            time.sleep(0.1)

    _patch_capability_convert(monkeypatch, "pdf_document", "text", _hang_forever)

    resp = client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("a.pdf", io.BytesIO(_good_pdf_bytes("Timeout Test")), "application/pdf")},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + 20
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.2)

    assert status == "failed", f"hung job never timed out (last status: {status})"
