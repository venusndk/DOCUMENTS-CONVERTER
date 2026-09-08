"""
PDF -> plain text (master directive Phase 9: "Core Conversion Engine").

Extracts each page's native text layer where the PDF has one; for a page
with no text layer, OCRs it instead (reusing this project's existing OCR
infrastructure -- Tesseract via pytesseract, the same 200 DPI rendering
default as searchable_pdf.py) rather than silently returning nothing for
exactly the kind of scanned document this whole project exists to
handle. A mixed PDF (see document_analysis.py's own "mixed"
classification) gets native extraction on its digital pages and OCR on
its scanned ones, page by page.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import fitz
import pytesseract
from PIL import Image

from ..registry import Capability

_DPI = 200


def _configure_tesseract_path() -> None:
    """Mirrors ocr_excel.convert_scanned_to_excel's and searchable_pdf's
    own handling of config.TESSERACT_CMD."""
    from ..api import config

    if config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
        tess_dir = os.path.dirname(config.TESSERACT_CMD)
        if tess_dir and tess_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    _configure_tesseract_path()
    doc = fitz.open(str(input_path))
    try:
        zoom = _DPI / 72
        matrix = fitz.Matrix(zoom, zoom)
        page_count = len(doc)
        page_texts = []
        for i in range(page_count):
            page = doc[i]
            native_text = page.get_text().strip()
            if native_text:
                progress(f"Extracting native text from page {i + 1}/{page_count}...")
                page_texts.append(native_text)
            else:
                progress(f"OCR page {i + 1}/{page_count} (no native text layer)...")
                pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                page_texts.append(pytesseract.image_to_string(image).strip())
    finally:
        doc.close()

    output_path.write_text("\n\n".join(page_texts), encoding="utf-8")
    progress("Done.")


CAPABILITY = Capability(
    source_format="pdf_document",
    target_format="text",
    description="PDF -> plain text: native text layer where present, OCR where not.",
    source_extensions=frozenset({".pdf"}),
    output_extension=".txt",
    media_type="text/plain",
    convert=_convert,
)
