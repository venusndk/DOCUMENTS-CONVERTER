"""
Image -> PDF: the one genuinely new conversion added in Phase 9, proving
the capability registry actually routes to something other than the
original OCR pipeline.

Deliberately simple and OCR-free (no table detection, no Tesseract
involved at all) -- the point of this conversion is to demonstrate that
the registry doesn't assume "everything is OCR", not to add a second
complex pipeline. Pillow (already a pinned dependency of the OCR
pipeline itself) can already write single-page PDFs directly from a
raster image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PIL import Image

from ..registry import Capability


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    progress("Reading image...")
    with Image.open(input_path) as img:
        # PDF has no alpha channel / palette concept -- flatten to RGB so
        # this doesn't fail (or silently mangle colors) on PNGs with
        # transparency or paletted GIFs/BMPs.
        rgb = img.convert("RGB")
        progress("Writing PDF...")
        rgb.save(output_path, "PDF")
    progress("Done.")


CAPABILITY = Capability(
    source_format="image",
    target_format="pdf",
    description="Image (PNG/JPEG/TIFF/BMP) -> single-page PDF. No OCR: a plain image-to-container conversion.",
    source_extensions=frozenset({".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}),
    output_extension=".pdf",
    media_type="application/pdf",
    convert=_convert,
)
