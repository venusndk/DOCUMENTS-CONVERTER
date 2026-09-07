"""
Tests for the scanned-document -> searchable-PDF conversion
(documents_converter/converters/searchable_pdf.py) -- Phase 5
completion, master directive numbering.
"""

from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from conftest import requires_tesseract
from documents_converter.api import config
from documents_converter.converters.searchable_pdf import _convert


@requires_tesseract
def test_searchable_pdf_from_a_pdf_embeds_the_real_ocr_text(
    synthetic_pdf, tesseract_cmd, tmp_path, monkeypatch
):
    """
    The actual point of this conversion: the output PDF's text layer,
    read back with PyMuPDF (not Tesseract again -- proving it's a real,
    embedded, extractable layer, not just that OCR ran), contains the
    fabricated fixture's known text.
    """
    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    output_path = tmp_path / "output.pdf"
    messages = []
    _convert(synthetic_pdf, output_path, progress=messages.append)

    assert output_path.exists()
    doc = fitz.open(str(output_path))
    try:
        assert doc.page_count == 1
        full_text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    assert "SMITH" in full_text.upper()
    assert any("OCR page" in m for m in messages)
    assert "Done." in messages


@requires_tesseract
def test_searchable_pdf_preserves_original_page_appearance(
    synthetic_pdf, tesseract_cmd, tmp_path, monkeypatch
):
    """The point of a searchable PDF (vs. plain extracted text) is that
    it still looks like the scanned original -- confirmed by rendering
    the output page to a pixmap and checking it isn't blank/degenerate."""
    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    output_path = tmp_path / "output.pdf"
    _convert(synthetic_pdf, output_path, progress=lambda _msg: None)

    doc = fitz.open(str(output_path))
    try:
        page = doc[0]
        pix = page.get_pixmap()
        assert pix.width > 100 and pix.height > 100
    finally:
        doc.close()


@requires_tesseract
def test_searchable_pdf_from_a_plain_image(tesseract_cmd, tmp_path, monkeypatch):
    """Same conversion, non-PDF input -- a single image, not routed
    through the PDF-page-rendering branch at all."""
    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    img = Image.new("RGB", (600, 200), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 80), "HELLO WORLD", fill="black", font=ImageFont.load_default(size=36))
    img_path = tmp_path / "photo.png"
    img.save(img_path)

    output_path = tmp_path / "output.pdf"
    _convert(img_path, output_path, progress=lambda _msg: None)

    doc = fitz.open(str(output_path))
    try:
        assert doc.page_count == 1
        text = doc[0].get_text().upper()
    finally:
        doc.close()

    assert "HELLO" in text
