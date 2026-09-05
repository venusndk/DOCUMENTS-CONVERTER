"""
Generates a synthetic "scanned" grade-sheet PDF for tests.

Deliberately does NOT use any real student data (see docs/PHASE_0_AUDIT.md
risk register item #1 -- real names/IDs/grades must never enter version
control). Everything here is fabricated, and the generator is small enough
to read and confirm that at a glance.

The image is rendered with PIL and saved through Pillow's own PDF writer,
so the resulting PDF has no text layer -- it's genuinely image-only, like
a real scan, forcing the OCR path to run rather than a native-text
shortcut. One header cell (MOD-A) is rendered pre-rotated 90 degrees and
made narrower than the other header cells, mirroring the real-world
layout (module names/codes printed sideways in a narrow column, next to
wider plain-text label cells) that _fix_rotated_cells()'s tall/narrow
aspect-ratio heuristic depends on -- a uniform-width header row would
never trigger it, so the column widths here are deliberately uneven.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

# Fabricated data only.
STUDENTS = [
    ("1", "100000001", "SMITH", "JOHN", "M", "85,00", "90,00", "175", "87,50"),
    ("2", "100000002", "DOE", "JANE", "F", "70,00", "65,00", "135", "67,50"),
]
COLUMNS = ["S/N", "Ref.No", "Surname", "Firstname", "Sex", "MOD-A", "MOD-B", "Total", "Average"]
ROTATED_COLUMN = "MOD-A"
ROTATED_COLUMN_LABEL = "Sideways Hdr"  # the text actually printed sideways in that cell


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def build_synthetic_scan(output_pdf_path: str, scale: int = 3) -> None:
    """Renders the fabricated table above to a single-page image-only PDF."""
    wide_col_w = 130 * scale
    narrow_col_w = 55 * scale  # only ROTATED_COLUMN uses this
    row_h = 40 * scale
    # Tall relative to narrow_col_w (ratio ~1.6, above the 1.3 threshold)
    # but well under wide_col_w (ratio ~0.68, correctly not "rotated").
    header_h = 90 * scale
    margin = 20 * scale

    col_widths = [narrow_col_w if c == ROTATED_COLUMN else wide_col_w for c in COLUMNS]
    col_x = [margin]
    for w in col_widths:
        col_x.append(col_x[-1] + w)

    width = col_x[-1] + margin
    height = margin * 2 + header_h + row_h * (len(STUDENTS) * 2)

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = _font(11 * scale)
    small_font = _font(9 * scale)

    # Grid lines.
    y0 = margin
    y1 = height - margin
    n_rows = 1 + len(STUDENTS) * 2  # header row + 2 rows per student
    for r in range(n_rows + 1):
        y = y0 + (header_h if r > 0 else 0) + max(r - 1, 0) * row_h
        draw.line([(col_x[0], y), (col_x[-1], y)], fill="black", width=max(scale // 2, 1))
    for x in col_x:
        draw.line([(x, y0), (x, y1)], fill="black", width=max(scale // 2, 1))

    # Header row: plain labels, except the narrow cell holds pre-rotated text.
    for c, label in enumerate(COLUMNS):
        cell_x, cell_w = col_x[c], col_widths[c]
        if label == ROTATED_COLUMN:
            text_img = Image.new("RGB", (header_h - 10, cell_w - 10), "white")
            tdraw = ImageDraw.Draw(text_img)
            tdraw.text((5, 5), ROTATED_COLUMN_LABEL, fill="black", font=small_font)
            rotated = text_img.rotate(90, expand=True)
            img.paste(rotated, (cell_x + 5, y0 + 5))
        else:
            draw.text((cell_x + 5, y0 + header_h // 2), label, fill="black", font=font)

    # Data rows: each student gets a mark row and a blank-ish second row
    # (mirrors the real documents' "Letter Grade" sub-row pattern).
    y = y0 + header_h
    for student in STUDENTS:
        for c, value in enumerate(student):
            draw.text((col_x[c] + 5, y + row_h // 3), value, fill="black", font=font)
        y += row_h
        y += row_h  # second (blank) row per student

    img.save(output_pdf_path, "PDF", resolution=200.0)


if __name__ == "__main__":
    import sys

    build_synthetic_scan(sys.argv[1] if len(sys.argv) > 1 else "synthetic_scan.pdf")
