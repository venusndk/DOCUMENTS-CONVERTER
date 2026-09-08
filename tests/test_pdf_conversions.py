"""
Tests for the PDF -> images and PDF -> text capabilities
(documents_converter/converters/pdf_to_images.py, pdf_to_text.py) --
Phase 9, master directive numbering. Neither needs LibreOffice.
"""

from __future__ import annotations

import io
import zipfile

import fitz
import pytest
from PIL import Image

from conftest import requires_tesseract
from documents_converter.api import config
from documents_converter.converters.pdf_to_images import _convert as pdf_to_images_convert
from documents_converter.converters.pdf_to_text import _convert as pdf_to_text_convert


def _build_two_page_pdf(path, page_texts: list[str]) -> None:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page(width=300, height=200)
        page.insert_text((36, 100), text)
    doc.save(str(path))
    doc.close()


def test_pdf_to_images_produces_one_png_per_page(tmp_path):
    pdf_path = tmp_path / "two_pages.pdf"
    _build_two_page_pdf(pdf_path, ["Page one.", "Page two."])

    output_path = tmp_path / "output.zip"
    messages = []
    pdf_to_images_convert(pdf_path, output_path, progress=messages.append)

    assert output_path.exists()
    with zipfile.ZipFile(output_path) as zf:
        names = sorted(zf.namelist())
        assert names == ["page-001.png", "page-002.png"]
        for name in names:
            img = Image.open(io.BytesIO(zf.read(name)))
            assert img.format == "PNG"
            assert img.width > 0 and img.height > 0
    assert "Done." in messages


def test_pdf_to_images_single_page_still_produces_a_zip(tmp_path):
    pdf_path = tmp_path / "one_page.pdf"
    _build_two_page_pdf(pdf_path, ["Only page."])

    output_path = tmp_path / "output.zip"
    pdf_to_images_convert(pdf_path, output_path, progress=lambda _m: None)

    with zipfile.ZipFile(output_path) as zf:
        assert zf.namelist() == ["page-001.png"]


def test_pdf_to_text_extracts_native_text_layer(tmp_path):
    pdf_path = tmp_path / "digital.pdf"
    _build_two_page_pdf(pdf_path, ["First page text.", "Second page text."])

    output_path = tmp_path / "output.txt"
    pdf_to_text_convert(pdf_path, output_path, progress=lambda _m: None)

    text = output_path.read_text(encoding="utf-8")
    assert "First page text." in text
    assert "Second page text." in text


@requires_tesseract
def test_pdf_to_text_ocrs_pages_with_no_text_layer(tmp_path, synthetic_pdf, tesseract_cmd, monkeypatch):
    """The synthetic_scan.pdf fixture is genuinely image-only (no text
    layer at all, by construction -- see its own docstring), so this
    exercises the actual OCR fallback path, not the native-extraction
    shortcut."""
    monkeypatch.setattr(config, "TESSERACT_CMD", tesseract_cmd)

    output_path = tmp_path / "output.txt"
    pdf_to_text_convert(synthetic_pdf, output_path, progress=lambda _m: None)

    text = output_path.read_text(encoding="utf-8")
    assert "SMITH" in text.upper()
