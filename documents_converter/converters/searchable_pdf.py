"""
Scanned document -> searchable PDF (master directive Phase 5 completion:
OCR Engine's "searchable PDF support where appropriate").

Renders each page (a PDF's pages via PyMuPDF, or a single image, at the
same 200 DPI default this project's own testing already found correct
for Tesseract's accuracy -- see README's `--dpi` note) and re-OCRs it
through Tesseract's own PDF output mode
(`pytesseract.image_to_pdf_or_hocr`), which produces a single-page PDF
with an invisible text layer positioned over the original page image:
the result looks identical to the scanned original, but its text is now
selectable, searchable, and copyable in any PDF viewer. Per-page PDFs
are merged into one document with PyMuPDF.

Deliberately does NOT reuse HighResPDF (documents_converter/providers/
table_detection.py): that class is an img2table.document.PDF subclass
built for table-detection's own needs (bordered/borderless mode,
rotation-detection integration). Dragging in that whole machinery for a
conversion that needs nothing but "render each page to an image" would
be needless coupling -- this module renders pages directly with
PyMuPDF instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import fitz
import pytesseract
from PIL import Image

from ..registry import Capability

# Same default and same reasoning as HighResPDF's own `dpi` field: 200 is
# what this project's own testing against a real document found correct
# for Tesseract's accuracy -- raising it can make table/line detection
# elsewhere in this project miss things, though that specific finding
# doesn't apply to plain full-page OCR the way it does to table-grid
# detection. Kept the same for consistency rather than introducing a
# second, untested default.
_DPI = 200


def _configure_tesseract_path() -> None:
    """Mirrors ocr_excel.convert_scanned_to_excel's own handling of
    config.TESSERACT_CMD: pytesseract needs its own tesseract_cmd set
    (used by image_to_pdf_or_hocr below), and the binary's directory
    needs to be on PATH too, matching the existing convention elsewhere
    in this project for a non-PATH Tesseract install."""
    from ..api import config

    if config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
        tess_dir = os.path.dirname(config.TESSERACT_CMD)
        if tess_dir and tess_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")


def _ocr_page_to_pdf_bytes(image: Image.Image) -> bytes:
    """One rendered page -> a single-page PDF with an invisible OCR text
    layer, via Tesseract's own PDF output mode (not this project's usual
    per-cell OCR path -- there's no table structure to preserve here,
    just the whole page's text)."""
    return pytesseract.image_to_pdf_or_hocr(image, extension="pdf")


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    _configure_tesseract_path()

    merged = fitz.open()
    try:
        if input_path.suffix.lower() == ".pdf":
            src = fitz.open(str(input_path))
            try:
                zoom = _DPI / 72
                matrix = fitz.Matrix(zoom, zoom)
                page_count = len(src)
                for i in range(page_count):
                    progress(f"OCR page {i + 1}/{page_count}...")
                    pix = src[i].get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
                    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    page_pdf_bytes = _ocr_page_to_pdf_bytes(image)
                    with fitz.open(stream=page_pdf_bytes, filetype="pdf") as page_doc:
                        merged.insert_pdf(page_doc)
            finally:
                src.close()
        else:
            progress("OCR page...")
            with Image.open(input_path) as img:
                image = img.convert("RGB")
                page_pdf_bytes = _ocr_page_to_pdf_bytes(image)
            with fitz.open(stream=page_pdf_bytes, filetype="pdf") as page_doc:
                merged.insert_pdf(page_doc)

        progress("Saving searchable PDF...")
        merged.save(str(output_path))
    finally:
        merged.close()

    progress("Done.")


CAPABILITY = Capability(
    source_format="scanned_document",
    target_format="searchable_pdf",
    description=(
        "Scanned PDF or image -> searchable PDF: the original page image, "
        "unchanged, with an invisible OCR text layer added so it's "
        "selectable/searchable/copyable. No table extraction -- see the "
        "xlsx target for that."
    ),
    source_extensions=frozenset(
        {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
    ),
    output_extension=".pdf",
    media_type="application/pdf",
    convert=_convert,
)
