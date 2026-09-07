import os
import shutil
import sys
import tempfile
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


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory) -> Path:
    """A small, fabricated (no real personal data) scanned-style PDF fixture."""
    from synthetic_scan import build_synthetic_scan

    out_dir = tmp_path_factory.mktemp("fixtures")
    pdf_path = out_dir / "synthetic_scan.pdf"
    build_synthetic_scan(str(pdf_path))
    return pdf_path
