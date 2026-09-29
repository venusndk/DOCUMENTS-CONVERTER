"""
Memory profiling (Phase 19, master directive numbering: Performance &
Security Hardening) -- real, measured memory behavior across many
repeated conversions through the actual synchronous HTTP endpoint, via
Python's own tracemalloc, not a design-review assumption that "the
storage/work_dir cleanup code looks right so there's no leak."

Deliberately uses image->pdf and pdf->text (no Tesseract/OCR) so this
runs everywhere this project's own suite does, the same reasoning
tests/test_job_queue_api.py gives for the same target choice. A
dedicated, separate, @requires_tesseract-marked test covers the OCR
pipeline specifically, since PIL/pytesseract/opencv's own native-buffer
lifetimes are a real, distinct risk this narrower one can't speak to.

The actual leak-detection heuristic: compare retained Python-heap size
(tracemalloc, which only sees Python-allocated objects -- real enough
for catching a reference cycle or an accumulating cache in this
project's own code, not for a leak inside a C extension's own
unmanaged memory) after a warm-up batch (lets one-time imports/caches
settle) against a later batch of the same size. A real leak grows
roughly linearly with iteration count; incidental per-run noise
(garbage not yet collected, allocator fragmentation) does not -- the
bound here is deliberately generous (not a tight regression gate on
exact byte counts, which would make this test fail on an unrelated,
harmless dependency-version bump) while still being real enough to
catch a genuine, unbounded per-request leak.
"""

from __future__ import annotations

import gc
import io
import tracemalloc

import fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from documents_converter.api.app import _rate_limiter, app

client = TestClient(app)

_WARMUP_ITERATIONS = 10
_MEASURED_ITERATIONS = 30
#: A genuinely unbounded per-request leak would dwarf this after 30
#: more requests -- generous on purpose, see module docstring.
_MAX_ACCEPTABLE_GROWTH_BYTES = 20 * 1024 * 1024


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch):
    """See tests/test_api.py's identical fixture -- also raises the
    configured limit for this file specifically: every test here makes
    _WARMUP_ITERATIONS + _MEASURED_ITERATIONS (or more) real requests
    from the same client, deliberately, to measure memory across many
    iterations -- the default RATE_LIMIT_MAX_REQUESTS (10/window) would
    otherwise start returning 429 partway through every single one of
    them, which isn't a memory finding, just this file's own request
    volume colliding with an unrelated, already-tested feature."""
    # _rate_limiter is a module-level singleton constructed once at
    # app.py import time with max_requests already baked in from
    # config.RATE_LIMIT_MAX_REQUESTS -- monkeypatching config itself
    # after that construction has no effect; the limiter's own
    # attribute is what actually gates every request.
    monkeypatch.setattr(_rate_limiter, "max_requests", 100_000)
    _rate_limiter._counts.clear()
    yield


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 100), "Memory profile test")
    data = doc.tobytes()
    doc.close()
    return data


def _image_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), color=(60, 90, 180)).save(buf, "PNG")
    return buf.getvalue()


def _convert_pdf_to_text():
    resp = client.post(
        "/api/v1/convert",
        data={"target": "text"},
        files={"file": ("mem.pdf", io.BytesIO(_pdf_bytes()), "application/pdf")},
    )
    assert resp.status_code == 200


def _convert_image_to_pdf():
    resp = client.post(
        "/api/v1/convert",
        data={"target": "pdf"},
        files={"file": ("mem.png", io.BytesIO(_image_bytes()), "image/png")},
    )
    assert resp.status_code == 200


def _measure_growth(convert_once) -> tuple[int, int]:
    """Runs `convert_once` _WARMUP_ITERATIONS times (unmeasured), snapshots,
    runs it _MEASURED_ITERATIONS more times, snapshots again. Returns
    (before_bytes, after_bytes) -- Python-heap size tracemalloc can
    actually see, each preceded by a real gc.collect() so a snapshot
    doesn't count garbage that's merely pending collection as "still
    retained"."""
    for _ in range(_WARMUP_ITERATIONS):
        convert_once()

    gc.collect()
    tracemalloc.start()
    before_current, _ = tracemalloc.get_traced_memory()

    for _ in range(_MEASURED_ITERATIONS):
        convert_once()

    gc.collect()
    after_current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return before_current, after_current


def test_repeated_pdf_to_text_conversions_do_not_leak_unboundedly():
    before, after = _measure_growth(_convert_pdf_to_text)
    growth = after - before
    assert growth < _MAX_ACCEPTABLE_GROWTH_BYTES, (
        f"{_MEASURED_ITERATIONS} more pdf->text conversions grew the Python "
        f"heap by {growth / 1_000_000:.1f} MB -- possible leak."
    )


def test_repeated_image_to_pdf_conversions_do_not_leak_unboundedly():
    before, after = _measure_growth(_convert_image_to_pdf)
    growth = after - before
    assert growth < _MAX_ACCEPTABLE_GROWTH_BYTES, (
        f"{_MEASURED_ITERATIONS} more image->pdf conversions grew the Python "
        f"heap by {growth / 1_000_000:.1f} MB -- possible leak."
    )


def test_work_directories_do_not_accumulate_on_disk():
    """The other real half of 'no leak' for this project specifically:
    storage.py's own work_dir cleanup (not tracemalloc's business, since
    these are real files on disk, not Python heap objects) actually
    removes every synchronous request's scratch directory. Counts real
    directories under the OS temp root before and after a batch of
    conversions."""
    import tempfile
    from pathlib import Path

    temp_root = Path(tempfile.gettempdir())

    def _count_docconv_dirs() -> int:
        return len([p for p in temp_root.glob("docconv-*") if p.is_dir()])

    before_count = _count_docconv_dirs()
    for _ in range(20):
        _convert_pdf_to_text()
    after_count = _count_docconv_dirs()

    assert after_count <= before_count, (
        f"work_dir count grew from {before_count} to {after_count} after 20 "
        "synchronous conversions -- storage.release() may not be cleaning up."
    )
