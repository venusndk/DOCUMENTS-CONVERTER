"""
Document analysis (master directive Phase 4, "Document Analysis").

Produces a DocumentAnalysis summary for an uploaded file -- per-page
digital/scanned classification for PDFs (including genuine "mixed"
detection, not just a whole-document guess), safe metadata, and a
deliberately conservative quality signal -- independent of, and before,
the OCR/table-detection pipeline (master directive Phases 5-7) actually
runs.

This is a NEW, separate module, not a replacement for
ocr_excel.is_scanned_pdf(): that function samples only the first few
pages for speed and returns a single whole-document yes/no, which is
exactly right for its one caller's actual need (deciding once whether
to run OCR at all) and is already tested/relied on -- changing its
behavior to serve a different purpose (per-page "mixed" classification)
risks a regression for no benefit. This module checks every page (page
counts are already capped well below any performance concern by
documents_converter/api/security.py's decompression-bomb limit) because
"mixed" classification is only meaningful with full coverage.

Quality signal is deliberately conservative. This project's own README
(the `--dpi`/`--preprocess` section) documents a real, tested lesson:
plausible-sounding image-quality heuristics did not actually predict OCR
outcomes on the one real document tested against, and shipped anyway
they would have been an untested guess presented as a real signal.
Rather than repeat that mistake with an elaborate, unvalidated quality
*score*, the flags here are limited to conditions with a direct,
mechanical link to a real problem (an empty document, an image too
small to plausibly contain readable text at all) -- not a tuned
prediction of OCR accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import fitz
from PIL import Image as PILImage

DocType = Literal["pdf", "image"]
Classification = Literal["digital", "scanned", "mixed", "image"]

# Below this in either dimension, an image is too small to plausibly
# contain a readable table/page of text regardless of source quality --
# not a prediction about OCR accuracy in general (see module docstring).
MIN_PLAUSIBLE_DIMENSION_PX = 200


@dataclass(frozen=True)
class PageAnalysis:
    index: int
    has_text_layer: bool
    width_pt: float
    height_pt: float


@dataclass(frozen=True)
class DocumentAnalysis:
    doc_type: DocType
    page_count: int
    classification: Classification
    pages: list[PageAnalysis] = field(default_factory=list)
    # Safe, caller-facing metadata (returned to whoever uploaded the file
    # -- it's their own document). Deliberately never written to the
    # audit trail (documents_converter/api/audit.py): PDF producer/
    # creator/title fields can occasionally carry a real person's name,
    # and this project's own policy (docs/PHASE_0_AUDIT.md Risk Register
    # #1) is that server-side logs never carry anything read from inside
    # a document.
    metadata: dict = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)


def analyze_pdf(path: Path) -> DocumentAnalysis:
    doc = fitz.open(str(path))
    try:
        pages = [
            PageAnalysis(
                index=i,
                has_text_layer=bool(page.get_text().strip()),
                width_pt=page.rect.width,
                height_pt=page.rect.height,
            )
            for i, page in enumerate(doc)
        ]
        raw_metadata = doc.metadata or {}
        encrypted = doc.is_encrypted
    finally:
        doc.close()

    n_digital = sum(1 for p in pages if p.has_text_layer)
    if not pages:
        classification: Classification = "scanned"  # nothing to call digital
    elif n_digital == 0:
        classification = "scanned"
    elif n_digital == len(pages):
        classification = "digital"
    else:
        classification = "mixed"

    quality_flags = []
    if not pages:
        quality_flags.append("no_pages")

    metadata = {
        "producer": raw_metadata.get("producer") or None,
        "creator": raw_metadata.get("creator") or None,
        "creation_date": raw_metadata.get("creationDate") or None,
        "encrypted": encrypted,
    }

    return DocumentAnalysis(
        doc_type="pdf",
        page_count=len(pages),
        classification=classification,
        pages=pages,
        metadata=metadata,
        quality_flags=quality_flags,
    )


def analyze_image(path: Path) -> DocumentAnalysis:
    with PILImage.open(path) as img:
        width, height = img.size
        fmt = img.format
        mode = img.mode
        dpi = img.info.get("dpi")

    quality_flags = []
    if width < MIN_PLAUSIBLE_DIMENSION_PX or height < MIN_PLAUSIBLE_DIMENSION_PX:
        quality_flags.append("very_low_resolution")

    metadata = {
        "format": fmt,
        "mode": mode,
        "width": width,
        "height": height,
        "dpi": list(dpi) if dpi else None,
    }

    return DocumentAnalysis(
        doc_type="image",
        page_count=1,
        classification="image",
        pages=[],
        metadata=metadata,
        quality_flags=quality_flags,
    )


def analyze(path: Path, ext: str) -> DocumentAnalysis:
    """Dispatches on extension -- the same `ext` callers already have
    from validating the upload (documents_converter/api/app.py)."""
    if ext == ".pdf":
        return analyze_pdf(path)
    return analyze_image(path)
