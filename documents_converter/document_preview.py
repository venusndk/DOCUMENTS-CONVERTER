"""
Document preview (master directive Phase 15: "Document Preview &
Human Review" -- PDF preview, OCR preview, extracted text preview,
table preview, editable extracted tables, suspicious-cell review).

The last two items -- editable extracted tables, suspicious-cell
review -- were already built in Phase 7 completion (master directive
numbering): documents_converter/ocr_excel.py's review-JSON snapshot and
documents_converter/api/app.py's GET/POST /api/v1/jobs/{id}/review,
with a contenteditable table UI in the frontend (amber highlight +
warning icon for a suspicious cell). What Phase 7 didn't build is any
visual reference to the actual source page while reviewing -- a
reviewer correcting a flagged cell could see the extracted text and
guess, but never the real page it came from. This module is what fixes
that, plus a standalone, job-independent preview for a document nobody
has committed to converting yet.

Two capabilities, both new:

- render_page_image(): renders one page of an uploaded PDF (or an
  image upload's single page) to a PNG, at this project's own
  established 200 DPI standard (pdf_to_images.py, searchable_pdf.py) --
  used by GET /api/v1/jobs/{id}/preview/pages/{page_number} to pair a
  real page image with that job's review data. Every review-JSON
  table's `sheet_name` already encodes its 1-indexed source page
  ("Page N - Table M" -- see ocr_excel.py's _write_table_flagged
  callers), so the frontend can request the matching page image with no
  new field needed on the review JSON itself.

- extract_text_preview(): a bounded preview of one page's text --
  native text layer if the page has one, real Tesseract OCR (same
  200 DPI rendering, same config path as every other OCR call in this
  project) if it doesn't, always for an image upload. Backs
  POST /api/v1/preview, a job-independent "what would this look like"
  check before committing to a full conversion or job -- covers PDF
  preview, OCR preview, and extracted text preview together, without
  needing a queued job at all.

Deliberately does NOT attempt a job-independent *table* preview (full
img2table detection is a real, non-trivial amount of work -- running it
twice, once for a "preview" and again for the real job, wastes exactly
the compute the async job/queue machinery exists to spare a caller from
paying synchronously): table preview is served by the job-based path
above instead, once a real job's already done the real detection.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import fitz
import pytesseract
from PIL import Image as PILImage

# Same rendering resolution as pdf_to_images.py/searchable_pdf.py --
# this project's own tested-correct default, not a fresh guess for yet
# another feature that renders a page.
_DPI = 200

# Preview text is meant to be a quick look, not the document -- bounded
# so a caller isn't surprised by an arbitrarily large response for a
# page that happens to be text-dense.
_MAX_PREVIEW_CHARS = 1000


def _configure_tesseract_path() -> None:
    """Mirrors ocr_excel.py/pdf_to_text.py/searchable_pdf.py's own
    identical handling of config.TESSERACT_CMD -- each OCR call site in
    this project configures this for itself rather than sharing one
    more import-time side effect between modules that otherwise have no
    reason to depend on each other."""
    from .api import config

    if config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
        tess_dir = os.path.dirname(config.TESSERACT_CMD)
        if tess_dir and tess_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")


def _page_count(input_path: Path, ext: str) -> int:
    if ext == ".pdf":
        doc = fitz.open(str(input_path))
        try:
            return len(doc)
        finally:
            doc.close()
    return 1


def _check_page_number(page_number: int, page_count: int) -> None:
    if page_number < 1 or page_number > page_count:
        raise ValueError(f"Page {page_number} is out of range (document has {page_count} page(s)).")


def render_page_image(input_path: Path, ext: str, page_number: int) -> bytes:
    """PNG bytes for one 1-indexed page of `input_path`. `page_number`
    must be 1 for a non-PDF (image) upload -- it has exactly one page."""
    page_count = _page_count(input_path, ext)
    _check_page_number(page_number, page_count)

    if ext != ".pdf":
        with PILImage.open(input_path) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG")
            return buf.getvalue()

    doc = fitz.open(str(input_path))
    try:
        zoom = _DPI / 72
        matrix = fitz.Matrix(zoom, zoom)
        pix = doc[page_number - 1].get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
        return pix.tobytes("png")
    finally:
        doc.close()


def extract_text_preview(input_path: Path, ext: str, page_number: int) -> tuple[str, bool, int]:
    """
    Returns (text, was_ocr, page_count) for one 1-indexed page: the
    native text layer if the page has one, real Tesseract OCR
    otherwise (always OCR for a non-PDF image upload). `text` is
    truncated to _MAX_PREVIEW_CHARS -- this is a quick look, not a
    substitute for the real /api/v1/jobs pipeline's full extraction.
    """
    page_count = _page_count(input_path, ext)
    _check_page_number(page_number, page_count)
    _configure_tesseract_path()

    if ext != ".pdf":
        with PILImage.open(input_path) as img:
            text = pytesseract.image_to_string(img).strip()
        return text[:_MAX_PREVIEW_CHARS], True, page_count

    doc = fitz.open(str(input_path))
    try:
        page = doc[page_number - 1]
        native_text = page.get_text().strip()
        if native_text:
            return native_text[:_MAX_PREVIEW_CHARS], False, page_count

        zoom = _DPI / 72
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
        image = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
        ocr_text = pytesseract.image_to_string(image).strip()
        return ocr_text[:_MAX_PREVIEW_CHARS], True, page_count
    finally:
        doc.close()
