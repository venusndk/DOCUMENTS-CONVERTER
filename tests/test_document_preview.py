"""
Tests for document preview (documents_converter/document_preview.py) --
Phase 15, master directive numbering (Document Preview & Human Review).

Fabricated fixtures only (docs/PHASE_0_AUDIT.md Risk Register #1): a
digital PDF built directly with PyMuPDF for the native-text-layer path,
and a scanned-style page (real, large, clear text rendered onto a
blank image via PIL, not just a blank image) for the real-OCR path --
mirrors tests/fixtures/synthetic_invoice.py's own approach to getting a
genuinely OCR-able image rather than an untestable blank one.
"""

from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from documents_converter import document_preview
from conftest import requires_tesseract


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _scanned_page_bytes(text: str, scale: int = 3) -> bytes:
    """A real (fabricated), OCR-able page image -- large, clear text on
    a plain background, not a blank canvas with nothing for Tesseract
    to actually read."""
    width, height = 400 * scale, 150 * scale
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20 * scale, 50 * scale), text, fill=(0, 0, 0), font=_font(24 * scale))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _build_pdf(path, page_kinds: list[tuple[str, str]]) -> None:
    """page_kinds: list of (kind, text) -- kind is "digital" (real
    embedded text) or "scanned" (image only, no text layer, but real
    OCR-able text drawn onto it)."""
    doc = fitz.open()
    for kind, text in page_kinds:
        page = doc.new_page(width=400, height=150)
        if kind == "digital":
            page.insert_text((30, 75), text)
        else:
            page.insert_image(page.rect, stream=_scanned_page_bytes(text))
    doc.save(str(path))
    doc.close()


# --------------------------------------------------------------------------
# render_page_image
# --------------------------------------------------------------------------


def test_render_page_image_returns_a_real_png_for_each_page(tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("digital", "Page One"), ("digital", "Page Two")])

    for page_number, expected_text in ((1, "Page One"), (2, "Page Two")):
        image_bytes = document_preview.render_page_image(pdf_path, ".pdf", page_number)
        img = Image.open(io.BytesIO(image_bytes))
        assert img.format == "PNG"
        # A real, distinguishing check beyond "it's a PNG": render at
        # 200 DPI from a 400x150pt page should be meaningfully larger
        # than the page's own point dimensions, confirming this is an
        # actual rendered page, not some fixed placeholder image.
        assert img.width > 400
        assert img.height > 150


def test_render_page_image_rejects_an_out_of_range_page(tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("digital", "Only Page")])
    with pytest.raises(ValueError, match="out of range"):
        document_preview.render_page_image(pdf_path, ".pdf", 2)


def test_render_page_image_works_for_a_plain_image_upload(tmp_path):
    image_path = tmp_path / "photo.png"
    Image.new("RGB", (300, 200), color=(200, 60, 60)).save(image_path)

    image_bytes = document_preview.render_page_image(image_path, ".png", 1)
    img = Image.open(io.BytesIO(image_bytes))
    assert img.format == "PNG"
    assert img.size == (300, 200)


def test_render_page_image_rejects_page_2_for_a_single_page_image(tmp_path):
    image_path = tmp_path / "photo.png"
    Image.new("RGB", (300, 200)).save(image_path)
    with pytest.raises(ValueError, match="out of range"):
        document_preview.render_page_image(image_path, ".png", 2)


# --------------------------------------------------------------------------
# extract_text_preview
# --------------------------------------------------------------------------


def test_extract_text_preview_uses_the_native_text_layer_when_present(tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("digital", "Native Text Here")])

    text, is_ocr, page_count = document_preview.extract_text_preview(pdf_path, ".pdf", 1)

    assert "Native Text Here" in text
    assert is_ocr is False
    assert page_count == 1


def test_extract_text_preview_rejects_an_out_of_range_page(tmp_path):
    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("digital", "Only Page")])
    with pytest.raises(ValueError, match="out of range"):
        document_preview.extract_text_preview(pdf_path, ".pdf", 5)


def test_extract_text_preview_truncates_to_the_bound(tmp_path, monkeypatch):
    full_text = "This sentence is definitely longer than the truncation bound used below."
    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("digital", full_text)])
    monkeypatch.setattr(document_preview, "_MAX_PREVIEW_CHARS", 20)

    text, is_ocr, _ = document_preview.extract_text_preview(pdf_path, ".pdf", 1)

    assert text == full_text[:20]
    assert is_ocr is False


@requires_tesseract
def test_extract_text_preview_ocrs_a_scanned_page(tmp_path, tesseract_cmd, monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("scanned", "Scanned Page Text")])

    text, is_ocr, page_count = document_preview.extract_text_preview(pdf_path, ".pdf", 1)

    assert "Scanned" in text
    assert is_ocr is True
    assert page_count == 1


@requires_tesseract
def test_extract_text_preview_ocrs_a_plain_image_upload(tmp_path, tesseract_cmd, monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    image_path = tmp_path / "scan.png"
    image_path.write_bytes(_scanned_page_bytes("Uploaded Image Text"))

    text, is_ocr, page_count = document_preview.extract_text_preview(image_path, ".png", 1)

    assert "Uploaded" in text
    assert is_ocr is True
    assert page_count == 1


@requires_tesseract
def test_extract_text_preview_mixed_pdf_picks_native_or_ocr_per_page(tmp_path, tesseract_cmd, monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    pdf_path = tmp_path / "doc.pdf"
    _build_pdf(pdf_path, [("digital", "Digital Page"), ("scanned", "Scanned Page Text")])

    text1, is_ocr1, page_count = document_preview.extract_text_preview(pdf_path, ".pdf", 1)
    text2, is_ocr2, _ = document_preview.extract_text_preview(pdf_path, ".pdf", 2)

    assert "Digital Page" in text1
    assert is_ocr1 is False
    assert "Scanned" in text2
    assert is_ocr2 is True
    assert page_count == 2
