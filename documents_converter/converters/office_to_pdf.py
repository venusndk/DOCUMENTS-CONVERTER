"""
Word/Excel/PowerPoint -> PDF (master directive Phase 9: "Core Conversion
Engine"), via LibreOffice headless (_libreoffice.py) -- the real,
industry-standard approach; there is no lightweight pure-Python option
that renders real Office files correctly.

Deliberately one capability for all three formats rather than three
separate ones: LibreOffice's own headless conversion auto-detects the
input format from the file itself, and the underlying call is identical
regardless of which Office application originally created the file --
three near-identical modules each calling the same function would just
be duplication with no real seam behind it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ._libreoffice import convert_via_libreoffice
from ..registry import Capability


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    from ..api import config

    progress("Converting via LibreOffice...")
    convert_via_libreoffice(input_path, output_path, soffice_cmd=config.LIBREOFFICE_CMD)
    progress("Done.")


CAPABILITY = Capability(
    source_format="office_document",
    target_format="pdf",
    description=(
        "Word, Excel, or PowerPoint document -> PDF, via LibreOffice headless. "
        "Auto-detects which of the three from the file itself."
    ),
    source_extensions=frozenset({".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}),
    output_extension=".pdf",
    media_type="application/pdf",
    convert=_convert,
)
