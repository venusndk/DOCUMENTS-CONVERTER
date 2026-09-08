"""
PDF -> page images, packaged as a ZIP (master directive Phase 9: "Core
Conversion Engine").

One PNG per page, rendered at the same 200 DPI default used throughout
this project (HighResPDF, searchable_pdf.py) for the same reason: that's
the value this project's own testing found correct, not a fresh guess.
Always a ZIP, even for a single-page PDF -- a consistent response shape
callers can rely on rather than "sometimes a raw image, sometimes an
archive" depending on page count.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Callable

import fitz

from ..registry import Capability

# Same default and reasoning as HighResPDF's own `dpi` field and
# searchable_pdf.py's _DPI.
_DPI = 200


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    doc = fitz.open(str(input_path))
    try:
        zoom = _DPI / 72
        matrix = fitz.Matrix(zoom, zoom)
        page_count = len(doc)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in range(page_count):
                progress(f"Rendering page {i + 1}/{page_count}...")
                pix = doc[i].get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
                zf.writestr(f"page-{i + 1:03d}.png", pix.tobytes("png"))
    finally:
        doc.close()
    progress("Done.")


CAPABILITY = Capability(
    source_format="pdf_document",
    target_format="images",
    description="PDF -> one PNG per page, packaged as a ZIP archive.",
    source_extensions=frozenset({".pdf"}),
    output_extension=".zip",
    media_type="application/zip",
    convert=_convert,
)
