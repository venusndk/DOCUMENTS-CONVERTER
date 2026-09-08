"""
PDF utilities (master directive Phase 11: "PDF Utilities" -- not to be
confused with this project's own earlier, differently-numbered "Phase
11", production-readiness hardening; see README's disambiguation note
for the full story on the two numbering schemes in this project).

Eleven small, independent PDF manipulation operations, all via PyMuPDF
(fitz) alone -- no new dependency, unlike Phases 9-10's LibreOffice
work. These deliberately do NOT go through the Capability/registry
pattern used everywhere else in this project (one source format -> one
target format, selected by a single `target` string): merge needs
multiple input files, and most of the others need an operation-specific
parameter (a rotation angle, a page range, watermark text) that a
single `target` field has no room for. Exposed as their own dedicated
endpoints instead (documents_converter/api/app.py's /api/v1/pdf/*
routes), not routed through /api/v1/convert or /api/v1/jobs.

Every function here is plain and synchronous, taking real Path objects
and doing real file I/O -- no async job-queue involvement (Phase 7's
machinery exists for genuinely slow OCR/LibreOffice conversions; every
operation here is a fast, local, in-process PyMuPDF manipulation with
nothing to gain from it).
"""

from __future__ import annotations

from pathlib import Path

import fitz


def parse_page_spec(spec: str, page_count: int) -> list[int]:
    """
    Parses a 1-indexed, comma-separated page spec (e.g. "1,3,5-7") into
    a 0-indexed list of page numbers, in the order given -- so the same
    parser also serves reorder_pages (order matters there), not just
    selection.

    :raises ValueError: for anything malformed or out of range, with a
        message identifying exactly what was wrong, rather than
        silently clamping or skipping a bad entry.
    """
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, _, end_str = part.partition("-")
            try:
                start, end = int(start_str), int(end_str)
            except ValueError:
                raise ValueError(f"Invalid page range '{part}'.") from None
            if start > end:
                raise ValueError(f"Invalid range '{part}': start must be <= end.")
            pages.extend(range(start, end + 1))
        else:
            try:
                pages.append(int(part))
            except ValueError:
                raise ValueError(f"Invalid page number '{part}'.") from None

    for p in pages:
        if p < 1 or p > page_count:
            raise ValueError(f"Page {p} is out of range (document has {page_count} pages).")
    return [p - 1 for p in pages]


def merge_pdfs(input_paths: list[Path], output_path: Path) -> None:
    """Concatenates PDFs in the given order into one document."""
    merged = fitz.open()
    try:
        for path in input_paths:
            with fitz.open(str(path)) as doc:
                merged.insert_pdf(doc)
        merged.save(str(output_path))
    finally:
        merged.close()


def split_pdf(
    input_path: Path, output_dir: Path, ranges: list[list[int]] | None = None
) -> list[Path]:
    """
    Splits a PDF into multiple files. `ranges` is a list of 0-indexed
    page-index lists, one per output file; defaults to one file per
    page when not given. Returns the written file paths, in order.
    """
    doc = fitz.open(str(input_path))
    try:
        if ranges is None:
            ranges = [[i] for i in range(len(doc))]
        written = []
        for i, page_indices in enumerate(ranges):
            part = fitz.open()
            for p in page_indices:
                part.insert_pdf(doc, from_page=p, to_page=p)
            out_path = output_dir / f"part-{i + 1:03d}.pdf"
            part.save(str(out_path))
            part.close()
            written.append(out_path)
        return written
    finally:
        doc.close()


def extract_pages(input_path: Path, output_path: Path, pages: list[int]) -> None:
    """Writes a new PDF containing just `pages` (0-indexed), in the
    given order -- may repeat or reorder pages, unlike delete_pages."""
    doc = fitz.open(str(input_path))
    try:
        out = fitz.open()
        for p in pages:
            out.insert_pdf(doc, from_page=p, to_page=p)
        out.save(str(output_path))
        out.close()
    finally:
        doc.close()


def reorder_pages(input_path: Path, output_path: Path, order: list[int]) -> None:
    """Writes a new PDF with pages in the given 0-indexed order --
    `order` must be a permutation of every page index (use
    extract_pages instead for a subset or for repeating a page)."""
    doc = fitz.open(str(input_path))
    try:
        if sorted(order) != list(range(len(doc))):
            raise ValueError(
                f"`order` must be a permutation of all {len(doc)} page(s); "
                "use the extract operation for a subset."
            )
        doc.select(order)
        doc.save(str(output_path))
    finally:
        doc.close()


def delete_pages(input_path: Path, output_path: Path, pages: list[int]) -> None:
    """Writes a new PDF with `pages` (0-indexed) removed."""
    doc = fitz.open(str(input_path))
    try:
        doc.delete_pages(pages)
        doc.save(str(output_path))
    finally:
        doc.close()


def rotate_pages(
    input_path: Path, output_path: Path, degrees: int, pages: list[int] | None = None
) -> None:
    """Rotates `pages` (0-indexed; every page if None) by `degrees`
    clockwise (must be a multiple of 90 -- PyMuPDF's own page rotation,
    like most PDF viewers' page-rotation metadata, only supports the
    four right-angle orientations)."""
    if degrees % 90 != 0:
        raise ValueError("Rotation must be a multiple of 90 degrees.")
    doc = fitz.open(str(input_path))
    try:
        target_pages = pages if pages is not None else range(len(doc))
        for i in target_pages:
            page = doc[i]
            page.set_rotation((page.rotation + degrees) % 360)
        doc.save(str(output_path))
    finally:
        doc.close()


def _fit_watermark_fontsize(text: str, page_rect: fitz.Rect, max_fontsize: float) -> float:
    """
    Shrinks from `max_fontsize` until `text`, centered on the page and
    rotated 45 degrees around that same center, actually stays on the
    physical page.

    Found the hard way, through two wrong attempts before this one, not
    assumed correct: sizing against the page's plain width doesn't work
    (insert_textbox just wraps to a second line instead of overflowing,
    which isn't the failure that actually happens here), and sizing
    against the page's raw diagonal length doesn't either -- a 45-degree
    line centered on the page reaches the *shorter* of the two page
    dimensions first, not the corner-to-corner diagonal, so a
    diagonal-based budget is too generous whenever the page isn't
    square and silently under-shrinks (confirmed by rendering to an
    image and visually finding the text still clipped at both ends
    despite "fitting" the wrong metric). The correct reach along a
    45-degree line from the center to where it exits a
    `min_dim`-tall/wide page is `min_dim * sqrt(2)`.
    """
    limit = min(page_rect.width, page_rect.height) * (2**0.5) * 0.85
    fontsize = max_fontsize
    while fontsize > 4:
        if fitz.get_text_length(text, fontname="helv", fontsize=fontsize) <= limit:
            break
        fontsize -= 1
    return fontsize


def add_watermark(input_path: Path, output_path: Path, text: str) -> None:
    """
    Stamps `text` diagonally, semi-transparent, across every page.
    Verified visually (rendered to an image and inspected during
    development, not just assumed to look right from the API call) that
    the text is fully visible and centered, not clipped at the page
    edge -- see _fit_watermark_fontsize's own note on the real bug that
    check caught.

    Uses insert_text with an explicitly computed origin (not
    insert_textbox's rect-based layout, which anchors the line near the
    top of its box rather than through the true center) so the rotation
    pivot and the text's own visual center coincide -- PyMuPDF's
    insert_text/insert_textbox only accept `rotate` in multiples of 90,
    so the actual 45-degree diagonal needs `morph` (a rotation matrix)
    instead.
    """
    doc = fitz.open(str(input_path))
    try:
        for page in doc:
            cx, cy = page.rect.width / 2, page.rect.height / 2
            max_fontsize = min(page.rect.width, page.rect.height) / 6
            fontsize = _fit_watermark_fontsize(text, page.rect, max_fontsize)
            text_width = fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
            origin = fitz.Point(cx - text_width / 2, cy + fontsize / 3)
            morph = (fitz.Point(cx, cy), fitz.Matrix(45))
            page.insert_text(
                origin,
                text,
                fontsize=fontsize,
                color=(0.6, 0.6, 0.6),
                fill_opacity=0.4,
                morph=morph,
            )
        doc.save(str(output_path))
    finally:
        doc.close()


def add_page_numbers(input_path: Path, output_path: Path, start: int = 1) -> None:
    """Stamps a page number at the bottom-center of every page,
    starting from `start`."""
    doc = fitz.open(str(input_path))
    try:
        for i, page in enumerate(doc):
            label = str(start + i)
            rect = page.rect
            page.insert_textbox(
                fitz.Rect(0, rect.height - 30, rect.width, rect.height - 10),
                label,
                fontsize=10,
                align=fitz.TEXT_ALIGN_CENTER,
            )
        doc.save(str(output_path))
    finally:
        doc.close()


def crop_pages(
    input_path: Path, output_path: Path, margins: tuple[float, float, float, float]
) -> None:
    """Crops every page's visible area inward by `margins` (left, top,
    right, bottom), in points (72 per inch)."""
    left, top, right, bottom = margins
    doc = fitz.open(str(input_path))
    try:
        for page in doc:
            r = page.rect
            new_box = fitz.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom)
            if new_box.is_empty or not new_box.is_valid:
                raise ValueError(
                    f"Margins {margins} leave nothing visible on a "
                    f"{r.width}x{r.height}pt page."
                )
            page.set_cropbox(new_box)
        doc.save(str(output_path))
    finally:
        doc.close()


def compress_pdf(input_path: Path, output_path: Path) -> None:
    """Re-saves the PDF with structural cleanup and stream compression
    -- genuine size reduction for a PDF with redundant objects or
    uncompressed streams; a PDF that's already tightly optimized (e.g.
    one this project itself just wrote) may not shrink further, and
    that's disclosed rather than promised as a guaranteed size cut."""
    doc = fitz.open(str(input_path))
    try:
        doc.save(str(output_path), garbage=4, deflate=True, clean=True)
    finally:
        doc.close()


def repair_pdf(input_path: Path, output_path: Path) -> None:
    """Re-saves a PDF through PyMuPDF's own parser, which repairs many
    structural issues (broken xref tables, malformed object streams) as
    a side effect of successfully opening the file at all. A PDF too
    damaged for PyMuPDF to open at all raises before this function is
    even reached -- there's no separate, deeper recovery attempted."""
    doc = fitz.open(str(input_path))
    try:
        doc.save(str(output_path), garbage=4, clean=True)
    finally:
        doc.close()
