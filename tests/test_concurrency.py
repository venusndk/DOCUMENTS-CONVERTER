"""
Concurrency testing (Phase 19, master directive numbering: Performance
& Security Hardening) -- real simultaneous requests against shared
state, via a real thread pool, not a single-threaded assumption that
"the code looks thread-safe."

The one real bug this pass found: AccountStore.signup (accounts.py) had
a genuine check-then-insert race -- its own pre-check and the eventual
INSERT aren't atomic, so two truly concurrent signups for the same
email could both pass the check before either committed. Reproduced
directly (10 concurrent signups for one email, real ThreadPoolExecutor
threads): 9 of 10 crashed with a raw, unhandled
sqlite3.IntegrityError/psycopg.errors.UniqueViolation instead of the
clean EmailAlreadyRegisteredError every non-racing caller already got.
Fixed by catching IntegrityError around the commit and converting it to
that same clean error -- the database's own UNIQUE constraint was
always the real, final guard here; this just makes its failure mode
match the pre-check's.
"""

from __future__ import annotations

import io
import uuid
from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api.accounts import EmailAlreadyRegisteredError, accounts
from documents_converter.api.app import _rate_limiter, app
from documents_converter.api.rate_limit import FixedWindowRateLimiter

from conftest import requires_redis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch):
    """See tests/test_api.py's identical fixture -- also raises the
    real, already-constructed limiter's own max_requests (monkeypatching
    config.RATE_LIMIT_MAX_REQUESTS itself would do nothing; the
    singleton already captured that value at import time). Several
    tests below fire more real concurrent requests from one client than
    the default 10/window allows, deliberately, to test concurrency
    itself -- without this, some of those requests would get a real 429
    that looks like a concurrency bug but is actually just this file's
    own request volume colliding with an unrelated, already-tested
    feature (confirmed the hard way: an earlier version of
    test_concurrent_job_submissions_all_get_distinct_ids_and_succeed
    intermittently "failed" with a mix of 202s and 429s at the default
    limit for exactly this reason)."""
    monkeypatch.setattr(_rate_limiter, "max_requests", 100_000)
    _rate_limiter._counts.clear()
    yield


# --------------------------------------------------------------------------
# Signup race condition -- the real bug this phase found and fixed
# --------------------------------------------------------------------------


def test_concurrent_signup_for_the_same_email_never_crashes():
    """Real threads, real simultaneous signups, the same email -- exactly
    one must succeed, every other one must fail with the clean,
    documented error, and none may raise anything else."""
    email = f"race-{uuid.uuid4().hex}@example.com"
    outcomes = []

    def attempt(_i):
        try:
            accounts.signup(email, "a real password 123")
            return "success"
        except EmailAlreadyRegisteredError:
            return "rejected_cleanly"
        except Exception as e:  # pragma: no cover -- only hit on a real regression
            return f"CRASHED: {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(10)))

    crashes = [o for o in outcomes if o.startswith("CRASHED")]
    assert not crashes, f"unexpected crashes: {crashes}"
    assert outcomes.count("success") == 1
    assert outcomes.count("rejected_cleanly") == 9


def test_concurrent_signup_for_different_emails_all_succeed():
    """The fix above must not have turned a legitimate concurrent-but-
    non-colliding case into a false rejection -- confirms 10 distinct
    real signups, fired at the same time, all genuinely succeed."""

    def attempt(i):
        return accounts.signup(f"distinct-{uuid.uuid4().hex}-{i}@example.com", "a real password 123")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    assert len(results) == 10
    assert len({r.id for r in results}) == 10  # every account genuinely distinct


# --------------------------------------------------------------------------
# Rate limiter thread-safety
# --------------------------------------------------------------------------


def test_rate_limiter_stays_correct_under_real_concurrent_access():
    """FixedWindowRateLimiter.allow (rate_limit.py) uses a real Lock
    around its read-modify-write counter -- confirmed here under actual
    thread contention (50 threads, one shared key, all firing at once)
    rather than trusted from reading the source: exactly max_requests
    calls may return True, the rest False, with no double-counting or
    lost updates from the race itself."""
    limiter = FixedWindowRateLimiter(max_requests=20, window_seconds=60)

    def attempt(_i):
        return limiter.allow("shared-key")

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(attempt, range(50)))

    assert results.count(True) == 20
    assert results.count(False) == 30


# --------------------------------------------------------------------------
# Concurrent job submission
# --------------------------------------------------------------------------


def _build_pdf_bytes(text="Concurrency Test") -> bytes:
    doc = fitz.open()
    doc.new_page(width=200, height=100)
    doc[0].insert_text((20, 50), text)
    data = doc.tobytes()
    doc.close()
    return data


@requires_redis
def test_concurrent_job_submissions_all_get_distinct_ids_and_succeed():
    """Many real, simultaneous POST /api/v1/jobs requests through the
    actual HTTP layer (TestClient, real thread pool) -- confirms
    JobStore.create's uuid4-based id generation and the underlying
    database session handling hold up under real concurrent load: every
    request gets its own job id, no collisions, no cross-request state
    bleeding between them."""
    pdf_bytes = _build_pdf_bytes()

    def submit(_i):
        resp = client.post(
            "/api/v1/jobs",
            data={"target": "text"},
            files={"file": ("doc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        )
        return resp.status_code, resp.json().get("job_id")

    with ThreadPoolExecutor(max_workers=15) as pool:
        results = list(pool.map(submit, range(15)))

    statuses = [r[0] for r in results]
    job_ids = [r[1] for r in results]
    assert all(s == 202 for s in statuses), statuses
    assert len(set(job_ids)) == len(job_ids), "duplicate job ids under concurrent submission"
