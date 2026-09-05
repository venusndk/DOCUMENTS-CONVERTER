import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Let tests `import scan_to_excel` and `from synthetic_scan import ...`
# regardless of the directory pytest is invoked from.
for p in (REPO_ROOT, FIXTURES_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


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


@pytest.fixture(scope="session")
def synthetic_pdf(tmp_path_factory) -> Path:
    """A small, fabricated (no real personal data) scanned-style PDF fixture."""
    from synthetic_scan import build_synthetic_scan

    out_dir = tmp_path_factory.mktemp("fixtures")
    pdf_path = out_dir / "synthetic_scan.pdf"
    build_synthetic_scan(str(pdf_path))
    return pdf_path
