"""
PDF -> DOCX (master directive Phase 10: "PDF -> Office"), via
LibreOffice headless (_libreoffice.py) -- the same engine and helper
Phase 9's Office/HTML/Markdown -> PDF capabilities use, just the
reverse direction (--convert-to docx instead of --convert-to pdf).

Quality is genuinely asymmetric with the PDF-producing direction, and
that's disclosed here rather than glossed over: turning a PDF (a fixed
visual layout with no guaranteed structure) back into an *editable*
document means reconstructing structure LibreOffice's PDF import filter
can only guess at. Confirmed directly against a real converted file: a
simple text-based PDF's text comes through as genuinely editable text,
but positioned in floating text-box shapes anchored at the PDF's
original coordinates (preserving the visual layout), not as flowing
body paragraphs -- so python-docx's own `document.paragraphs` (which
only sees top-level body text) won't find it; the text is really there
in the document's XML, just inside a text-box shape. A complex
multi-column layout, or a scanned/image-only PDF (no text to
reconstruct at all), degrades further toward embedding the original
page images directly.

Deliberately NOT layered with this project's own OCR pipeline as a
"smarter" fallback for scanned PDFs: that would need inventing a new
scanned-PDF-to-editable-document strategy (laying OCR'd text back into
a real page layout) well beyond what's been verified this phase, not a
small addition. Flagged as a known limitation rather than quietly
built and left untested.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ._libreoffice import convert_via_libreoffice
from ..registry import Capability


def _convert(input_path: Path, output_path: Path, *, progress: Callable[[str], None]) -> None:
    from ..api import config

    progress("Converting via LibreOffice...")
    # infilter="writer_pdf_import": LibreOffice's default PDF handling
    # opens it as a Draw document (no docx export filter exists for
    # Draw) -- confirmed directly against a real container, not assumed
    # -- so PDF -> docx needs to be forced through Writer's own PDF
    # import instead. See _libreoffice.py's own docstring for the full
    # story.
    convert_via_libreoffice(
        input_path,
        output_path,
        target_format="docx",
        infilter="writer_pdf_import",
        soffice_cmd=config.LIBREOFFICE_CMD,
    )
    progress("Done.")


CAPABILITY = Capability(
    source_format="pdf_document",
    target_format="docx",
    description=(
        "PDF -> DOCX, via LibreOffice headless. Best-effort: a simple text-based PDF "
        "converts to a reasonably editable document; a complex layout or a scanned/"
        "image-only PDF converts to a .docx that mostly just embeds the original "
        "page images, not an editable transcription."
    ),
    source_extensions=frozenset({".pdf"}),
    output_extension=".docx",
    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    convert=_convert,
)
