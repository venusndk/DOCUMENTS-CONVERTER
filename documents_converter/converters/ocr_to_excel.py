"""
Wraps the existing OCR->Excel pipeline (documents_converter/ocr_excel.py,
built across Phases 0-8) as one Capability for the registry.

The registry's ConvertFn signature is deliberately generic
(input_path, output_path, *, progress) so any future conversion can plug
in without the registry knowing anything about OCR specifically. This
wrapper's only job is adapting convert_scanned_to_excel's richer,
OCR-specific signature (tesseract_cmd, etc.) to that generic shape --
config lookups like TESSERACT_CMD stay here, not in registry.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..ocr_excel import convert_scanned_to_excel
from ..registry import Capability


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    # Local import: keeps this module from forcing a dependency on the API
    # package for anything (e.g. a future CLI path) that only wants the
    # conversion itself, not the web app.
    from ..api import config

    convert_scanned_to_excel(
        file_path=str(input_path),
        output_excel_path=str(output_path),
        tesseract_cmd=config.TESSERACT_CMD,
        progress=progress,
    )


CAPABILITY = Capability(
    source_format="scanned_document",
    target_format="xlsx",
    description="Scanned PDF or photographed table image -> Excel, via OCR and table detection.",
    source_extensions=frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}),
    output_extension=".xlsx",
    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    convert=_convert,
)
