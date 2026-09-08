"""
Generates a synthetic invoice-style table image for tests (Phase 8
completion, master directive numbering).

Two things this project's own audit (docs/PHASE_0_AUDIT.md) flagged as
unverified, addressed by this one fixture:

1. WEBP support -- saved as WEBP (lossless, so compression artifacts
   don't corrupt the crisp text edges OCR depends on), not PDF, forcing
   Pillow's WEBP encoder/decoder and the img2table.Image code path
   (rather than HighResPDF) to actually run.
2. A genuinely different table layout than the transcript-style grade
   sheet tested everywhere else in this project (tests/fixtures/
   synthetic_scan.py) -- a simple item/quantity/price/total table, the
   shape of an invoice or receipt line-item table, not a dense
   multi-column academic record.

Deliberately fabricated data only, matching every other fixture in this
project (see docs/PHASE_0_AUDIT.md Risk Register #1) -- this is not a
real invoice from any real business.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

# Fabricated data only -- no real business, product, or price.
ITEMS = [
    ("Widget A", "3", "10.00", "30.00"),
    ("Widget B", "2", "25.00", "50.00"),
    ("Gadget C", "1", "99.99", "99.99"),
]
COLUMNS = ["Item", "Qty", "Unit Price", "Total"]


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def build_synthetic_invoice(output_path: str, scale: int = 3) -> None:
    """Renders the fabricated table above to a single-page WEBP image."""
    col_w = 130 * scale
    row_h = 40 * scale
    margin = 20 * scale

    col_x = [margin + i * col_w for i in range(len(COLUMNS) + 1)]
    width = col_x[-1] + margin
    n_rows = len(ITEMS) + 1  # header + one row per item
    height = margin * 2 + row_h * n_rows

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = _font(11 * scale)

    y0 = margin
    for r in range(n_rows + 1):
        y = y0 + r * row_h
        draw.line([(col_x[0], y), (col_x[-1], y)], fill="black", width=max(scale // 2, 1))
    for x in col_x:
        draw.line([(x, y0), (x, y0 + row_h * n_rows)], fill="black", width=max(scale // 2, 1))

    for c, label in enumerate(COLUMNS):
        draw.text((col_x[c] + 5, y0 + row_h // 3), label, fill="black", font=font)

    y = y0 + row_h
    for item in ITEMS:
        for c, value in enumerate(item):
            draw.text((col_x[c] + 5, y + row_h // 3), value, fill="black", font=font)
        y += row_h

    # lossless=True: this is a crisp black-on-white synthetic table, not
    # a photo -- lossy WEBP compression would introduce exactly the kind
    # of edge artifacts that hurt OCR, which would make this test about
    # WEBP compression quality rather than about WEBP support.
    img.save(output_path, "WEBP", lossless=True)


if __name__ == "__main__":
    import sys

    build_synthetic_invoice(sys.argv[1] if len(sys.argv) > 1 else "synthetic_invoice.webp")
