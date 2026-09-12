import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Let tests `import scan_to_excel` and `from synthetic_scan import ...`
# regardless of the directory pytest is invoked from.
for p in (REPO_ROOT, FIXTURES_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Phase 1 completion (documents_converter/api/db.py): point the test
# session at an isolated temp SQLite file instead of the real
# ./data/documents_converter.db a local/dev run would use -- the engine
# is created once, at documents_converter.api.db's import time, so this
# MUST run before anything below (or any test module) imports
# documents_converter for the first time. setdefault(), not direct
# assignment, so CI or a developer can still point tests at a real
# Postgres instance explicitly (e.g. to verify that backend too) by
# setting DATABASE_URL before invoking pytest.
if "DATABASE_URL" not in os.environ:
    _test_db_dir = tempfile.mkdtemp(prefix="docconv-test-db-")
    os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_dir}/test.db"


def _find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    # Common Windows install location, matching this project's own README.
    windows_default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if Path(windows_default).is_file():
        return windows_default
    return None


TESSERACT_CMD = _find_tesseract()

requires_tesseract = pytest.mark.skipif(
    TESSERACT_CMD is None,
    reason="Tesseract OCR not found on PATH or at the default Windows install path",
)


@pytest.fixture(scope="session")
def tesseract_cmd() -> str:
    assert TESSERACT_CMD is not None
    return TESSERACT_CMD


def _find_libreoffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    # Common Windows install locations -- not installed on this project's
    # own dev machine as of Phase 9 (master directive numbering); real
    # verification for the Office/HTML/Markdown -> PDF capabilities that
    # need it happens in Docker (where it's apt-installed) and CI, not
    # locally. Listed anyway so a Windows dev machine that does have it
    # installed picks it up automatically.
    for candidate in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


LIBREOFFICE_CMD = _find_libreoffice()

requires_libreoffice = pytest.mark.skipif(
    LIBREOFFICE_CMD is None,
    reason="LibreOffice (soffice) not found on PATH or at a default install path",
)


@pytest.fixture(scope="session")
def libreoffice_cmd() -> str:
    assert LIBREOFFICE_CMD is not None
    return LIBREOFFICE_CMD


def _redis_is_reachable() -> bool:
    """Phase 14 (master directive numbering): the job queue
    (documents_converter/api/job_queue.py) needs a real, live Redis --
    unlike Tesseract/LibreOffice, there's no "not installed" file-path
    check possible here, just an actual connection attempt with a short
    timeout so a genuinely absent Redis fails fast rather than hanging
    the whole collection step."""
    try:
        from documents_converter.api.job_queue import get_redis_connection

        # Same connection (protocol=2, etc. -- see job_queue.py's own
        # notes) every real call site uses, not a separately constructed
        # one that could quietly drift from it and answer a different
        # question than "can the app actually talk to Redis".
        get_redis_connection().ping()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_is_reachable(),
    reason="No live Redis reachable at REDIS_URL -- not installed on this project's own dev "
    "machine (same gap as Tesseract/LibreOffice); real verification happens in Docker "
    "Compose and CI, where a redis service container is provisioned.",
)


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_database():
    """
    Ensures the jobs table exists before any test runs, by calling the
    exact same migration function (documents_converter.api.app.
    _run_migrations) the real app calls from its FastAPI lifespan handler
    at startup -- not a schema shortcut like Base.metadata.create_all(),
    since the point is exercising the real migration files too, not just
    getting a jobs table into place by some other means.

    Called directly rather than relying on the lifespan actually firing,
    because it doesn't: this project's test suite uses a bare
    `TestClient(app)` (not `with TestClient(app) as client:`), and
    confirmed empirically that FastAPI/Starlette's TestClient never runs
    ASGI lifespan events under that usage -- every job-related test failed
    with "no such table: jobs" before this fixture was added. Real
    uvicorn-served usage (this project's actual Docker/production path,
    and tests/test_frontend.py's own uvicorn.Server) is unaffected; this
    fixture only covers the gap in how the test suite talks to the app.
    """
    from documents_converter.api.app import _run_migrations

    _run_migrations()


@pytest.fixture(scope="session", autouse=True)
def _rq_worker_thread():
    """
    Phase 14 (master directive numbering): every @requires_redis test
    enqueues a real job onto a real Redis queue -- unlike the
    ThreadPoolExecutor this replaced, nothing processes that queue by
    itself. Runs an RQ SimpleWorker in a background thread for the
    whole test session so those tests' jobs actually complete.

    SimpleWorker, not the regular Worker: SimpleWorker executes each
    job in the worker's own process/thread instead of forking a child
    one. Two reasons that matters here, not just one: Worker's
    fork-per-job model doesn't exist on Windows (this project's own dev
    machine) at all, and even where forking does work, a forked child
    wouldn't see this test session's own monkeypatches (e.g.
    test_get_job_result_409_before_job_completes's injected slow
    conversion function) -- those only exist in this process's memory.

    A no-op when Redis isn't reachable: every test that would need this
    is already skipped via @requires_redis, so there is nothing for it
    to do, and constructing a Queue/connection here unconditionally
    would be pure overhead (and, if REDIS_URL pointed somewhere genuinely
    unreachable rather than just "nothing listening", a slow one).
    """
    if not _redis_is_reachable():
        yield
        return

    from documents_converter.api import job_queue
    from rq.timeouts import TimerDeathPenalty
    from rq.worker import SimpleWorker

    # This fixture polls every ~1s for the entire test session, and
    # every single poll -- even one that finds nothing queued -- logs a
    # full worker lifecycle at INFO (registering, subscribing to its
    # pubsub channel, "*** Listening...", acquiring the scheduler lock,
    # unsubscribing, "done, quitting"). Confirmed the hard way in CI,
    # not just suspected: a real run's "Run tests" step was truncated
    # for exceeding GitHub Actions' per-step log size limit, entirely
    # full of these idle-poll cycles with the actual failure (if any)
    # pushed out of the visible/retained log. Quieted to WARNING here
    # rather than trying to reduce poll frequency alone, since even a
    # slower interval still accumulates to a large volume of pure noise
    # over a multi-minute session.
    logging.getLogger("rq.worker").setLevel(logging.WARNING)
    logging.getLogger("rq.scheduler").setLevel(logging.WARNING)

    class _ThreadSafeWorker(SimpleWorker):
        """
        Two separate signal-in-a-background-thread problems, not one --
        found one at a time, the hard way:

        1. SimpleWorker.work() unconditionally installs SIGINT/SIGTERM
           handlers, which raises ValueError("signal only works in main
           thread") the moment it's called from anywhere but the main
           thread. This fixture's first version crashed on its very
           first burst call, silently leaving every @requires_redis
           test's job stuck "queued" forever (a crashed background
           thread doesn't fail the test waiting on it, just hangs it
           until its own timeout). Fixed by skipping handler
           installation entirely -- this session doesn't need
           OS-signal-based shutdown for a worker anyway; it's stopped
           via `stop_event` below.

        2. SimpleWorker.death_penalty_class -- the mechanism that
           enforces a job's own job_timeout -- is `TimerDeathPenalty`
           (a plain threading.Timer, thread-safe) on Windows, but
           `UnixSignalDeathPenalty` (SIGALRM-based, main-thread-only) on
           Linux. This project's own dev machine is Windows, so fix #1
           above was verified working there -- but CI runs on Linux,
           where fix #1 alone wasn't enough: EVERY job execution still
           raised the identical ValueError from inside perform_job's own
           timeout handling this time, not from work()'s startup path,
           so every @requires_redis test still failed. Reproduced
           directly (not guessed at) by running this exact test suite,
           against a real Redis, inside a from-scratch Linux container
           replicating CI's own steps -- 21 failed, 225 passed, matching
           a real CI run exactly, with the actual traceback finally
           visible instead of hidden behind CI's log truncation and
           this session's lack of admin/log-download access. Forcing
           TimerDeathPenalty here makes this worker's behavior the same
           thread-safe one on both platforms, rather than accidentally
           depending on which OS happens to be running the tests.
        """

        def _install_signal_handlers(self):
            pass

        death_penalty_class = TimerDeathPenalty

    stop_event = threading.Event()

    def _run() -> None:
        worker = _ThreadSafeWorker([job_queue.get_queue()], connection=job_queue.get_redis_connection())
        while not stop_event.is_set():
            try:
                # with_scheduler=True: without it, a job retried with a
                # nonzero Retry(interval=...) (rq_tasks.py's automatic
                # retry) gets scheduled for later rather than re-queued
                # immediately, and nothing ever promotes it back to the
                # real queue -- confirmed the hard way (see
                # worker_main.py's identical note, added after this exact
                # gap left a retry-then-succeed test stuck on "queued"
                # forever despite the interval having long since elapsed).
                # burst=True keeps this call's own scheduler pass a single
                # one-shot sweep (acquire lock, promote anything due,
                # release, return) rather than spawning a separate
                # long-lived scheduler process, which would be the wrong
                # model for a polling loop like this one anyway.
                worker.work(burst=True, with_scheduler=True)
            except Exception:
                # A raised exception here (a transient Redis hiccup, a
                # scheduler lock race under CI's different timing/load --
                # never reproduced locally, but confirmed for real in CI:
                # a run failed 21 tests in a row, every one stuck at
                # "queued" forever, immediately after a stretch that
                # otherwise passed -- the exact signature of this loop
                # dying partway through the session and never recovering,
                # since an uncaught exception in a background thread just
                # kills that thread silently) must never take the whole
                # loop down with it. Every @requires_redis test for the
                # rest of the session depends on this thread staying
                # alive; one bad iteration should cost that iteration,
                # not every job submitted afterward. Printed (not
                # swallowed silently) so a real, recurring problem is
                # still visible in captured test output.
                import traceback

                traceback.print_exc()
            stop_event.wait(0.5)

    thread = threading.Thread(target=_run, daemon=True, name="test-rq-worker")
    thread.start()
    yield
    stop_event.set()
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory) -> Path:
    """A small, fabricated (no real personal data) scanned-style PDF fixture."""
    from synthetic_scan import build_synthetic_scan

    out_dir = tmp_path_factory.mktemp("fixtures")
    pdf_path = out_dir / "synthetic_scan.pdf"
    build_synthetic_scan(str(pdf_path))
    return pdf_path


@pytest.fixture(scope="session")
def synthetic_invoice_webp(tmp_path_factory) -> Path:
    """A small, fabricated invoice-style table, saved as WEBP -- Phase 8
    completion (master directive numbering): WEBP support and a
    genuinely different table layout than synthetic_pdf's transcript
    style, both verified in one fixture."""
    from synthetic_invoice import build_synthetic_invoice

    out_dir = tmp_path_factory.mktemp("fixtures")
    webp_path = out_dir / "synthetic_invoice.webp"
    build_synthetic_invoice(str(webp_path))
    return webp_path
