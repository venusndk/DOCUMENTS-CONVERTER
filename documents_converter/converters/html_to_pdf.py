"""
HTML -> PDF (master directive Phase 9: "Core Conversion Engine"), via
the same LibreOffice-headless helper office_to_pdf.py uses
(_libreoffice.py) -- avoids adding a second, different rendering engine
(e.g. a headless browser, or a library needing its own system graphics
libraries) just for this one format, since LibreOffice already renders
HTML acceptably for a "core conversion" use case. Not a full browser
engine -- fine for document-shaped HTML, not pixel-perfect for complex
modern CSS/JS-heavy pages, and that limitation is real, not assumed.
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
    source_format="html",
    target_format="pdf",
    description=(
        "HTML -> PDF, via LibreOffice headless. Not a full browser rendering "
        "engine -- fine for document-shaped HTML, not pixel-perfect for complex "
        "modern CSS/JS."
    ),
    source_extensions=frozenset({".html", ".htm"}),
    output_extension=".pdf",
    media_type="application/pdf",
    convert=_convert,
)
