"""
PDF -> PPTX (master directive Phase 10: "PDF -> Office"), via
LibreOffice headless (_libreoffice.py) -- same engine and helper as
pdf_to_docx.py, --convert-to pptx instead of docx. See that module's
own docstring for the same disclosed quality asymmetry: reconstructing
an *editable* presentation from a fixed-layout PDF is inherently
best-effort, most reliable for simple, mostly-text pages and weakest
for complex or scanned/image-only ones.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ._libreoffice import convert_via_libreoffice
from ..registry import Capability


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    from ..api import config

    progress("Converting via LibreOffice...")
    # infilter="impress_pdf_import": same reasoning as pdf_to_docx.py's
    # own comment -- forces PDF -> Impress import so a pptx export
    # filter is actually available.
    convert_via_libreoffice(
        input_path,
        output_path,
        target_format="pptx",
        infilter="impress_pdf_import",
        soffice_cmd=config.LIBREOFFICE_CMD,
    )
    progress("Done.")


CAPABILITY = Capability(
    source_format="pdf_document",
    target_format="pptx",
    description=(
        "PDF -> PPTX, via LibreOffice headless. Best-effort, same as PDF -> DOCX: "
        "reliable for simple text-based pages, weakest for complex layouts or "
        "scanned/image-only PDFs (which have no text to reconstruct at all)."
    ),
    source_extensions=frozenset({".pdf"}),
    output_extension=".pptx",
    media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    convert=_convert,
)
