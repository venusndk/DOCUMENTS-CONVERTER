"""
Markdown -> PDF (master directive Phase 9: "Core Conversion Engine").

Converts Markdown to HTML with the `markdown` package (pure Python,
no system dependencies), then feeds that HTML through the same
LibreOffice-headless helper html_to_pdf.py and office_to_pdf.py use
(_libreoffice.py) -- one rendering engine for every ->PDF conversion in
this package, not a separate one per source format.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

import markdown

from ._libreoffice import convert_via_libreoffice
from ..registry import Capability


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    from ..api import config

    progress("Rendering Markdown to HTML...")
    md_text = input_path.read_text(encoding="utf-8")
    html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    html_doc = f'<!doctype html><html><head><meta charset="utf-8"></head><body>{html_body}</body></html>'

    with tempfile.TemporaryDirectory(prefix="md2html-") as tmp_dir:
        # LibreOffice names its output after the input file's own stem
        # (see _libreoffice.py) -- named to match output_path's stem so
        # a glance at intermediate files during debugging isn't confusing.
        html_path = Path(tmp_dir) / f"{output_path.stem}.html"
        html_path.write_text(html_doc, encoding="utf-8")

        progress("Converting via LibreOffice...")
        convert_via_libreoffice(html_path, output_path, soffice_cmd=config.LIBREOFFICE_CMD)

    progress("Done.")


CAPABILITY = Capability(
    source_format="markdown",
    target_format="pdf",
    description="Markdown -> PDF, via HTML (the `markdown` package) then LibreOffice headless.",
    source_extensions=frozenset({".md", ".markdown"}),
    output_extension=".pdf",
    media_type="application/pdf",
    convert=_convert,
)
