"""
Tests for document analysis (documents_converter/document_analysis.py) --
Phase 4 completion, master directive numbering.

PDF fixtures are built directly with PyMuPDF (already a pinned
dependency) rather than reusing tests/fixtures/synthetic_scan.py: that
fixture is specifically an image-only (scanned-style) PDF, and these
tests need genuinely digital (real embedded text), genuinely scanned
(image only, no text layer), and mixed (one of each) documents -- all
fabricated, no real data, consistent with docs/PHASE_0_AUDIT.md's Risk
Register.
"""

from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image

from documents_converter.document_analysis import analyze, analyze_image, analyze_pdf


def _blank_page_image_bytes(width=200, height=150) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(250, 250, 250)).save(buf, "PNG")
    return buf.getvalue()


def _build_pdf(path, page_kinds: list[str]) -> None:
    """page_kinds: list of "digital" or "scanned" -- one page per entry."""
    doc = fitz.open()
    img_bytes = _blank_page_image_bytes()
    for kind in page_kinds:
        page = doc.new_page(width=300, height=200)
        if kind == "digital":
            page.insert_text((36, 100), "This page has real, embedded text.")
        else:
            page.insert_image(page.rect, stream=img_bytes)
    doc.save(str(path))
    doc.close()


def test_analyze_pdf_classifies_all_digital_pages_as_digital(tmp_path):
    pdf_path = tmp_path / "digital.pdf"
    _build_pdf(pdf_path, ["digital", "digital"])

    result = analyze_pdf(pdf_path)

    assert result.doc_type == "pdf"
    assert result.page_count == 2
    assert result.classification == "digital"
    assert all(p.has_text_layer for p in result.pages)


def test_analyze_pdf_classifies_all_scanned_pages_as_scanned(tmp_path):
    pdf_path = tmp_path / "scanned.pdf"
    _build_pdf(pdf_path, ["scanned", "scanned"])

    result = analyze_pdf(pdf_path)

    assert result.classification == "scanned"
    assert all(not p.has_text_layer for p in result.pages)


def test_analyze_pdf_classifies_a_combination_as_mixed(tmp_path):
    pdf_path = tmp_path / "mixed.pdf"
    _build_pdf(pdf_path, ["digital", "scanned", "digital"])

    result = analyze_pdf(pdf_path)

    assert result.classification == "mixed"
    assert result.page_count == 3
    assert [p.has_text_layer for p in result.pages] == [True, False, True]


def test_analyze_pdf_reports_page_dimensions(tmp_path):
    pdf_path = tmp_path / "digital.pdf"
    _build_pdf(pdf_path, ["digital"])

    result = analyze_pdf(pdf_path)

    assert result.pages[0].width_pt == 300
    assert result.pages[0].height_pt == 200


def test_analyze_pdf_metadata_excludes_nothing_sensitive_by_construction(tmp_path):
    """Confirms the exact, deliberately small set of metadata fields --
    a regression check against silently starting to surface a raw
    metadata dict with more fields than this project's audit-log policy
    (never document content/author-adjacent fields in server logs) was
    designed around. This just checks the *shape*, not the log --
    documents_converter/api/app.py never passes this dict to audit.log_event
    at all, which is the actual guarantee."""
    pdf_path = tmp_path / "digital.pdf"
    _build_pdf(pdf_path, ["digital"])

    result = analyze_pdf(pdf_path)

    assert set(result.metadata.keys()) == {"producer", "creator", "creation_date", "encrypted"}


def test_analyze_image_reports_dimensions_and_format(tmp_path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), color=(10, 20, 30)).save(img_path)

    result = analyze_image(img_path)

    assert result.doc_type == "image"
    assert result.classification == "image"
    assert result.page_count == 1
    assert result.metadata["width"] == 400
    assert result.metadata["height"] == 300
    assert result.metadata["format"] == "PNG"
    assert result.quality_flags == []


def test_analyze_image_flags_very_low_resolution(tmp_path):
    img_path = tmp_path / "tiny.png"
    Image.new("RGB", (50, 40), color=(10, 20, 30)).save(img_path)

    result = analyze_image(img_path)

    assert "very_low_resolution" in result.quality_flags


def test_analyze_dispatches_on_extension(tmp_path):
    pdf_path = tmp_path / "digital.pdf"
    _build_pdf(pdf_path, ["digital"])
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (300, 300)).save(img_path)

    assert analyze(pdf_path, ".pdf").doc_type == "pdf"
    assert analyze(img_path, ".png").doc_type == "image"
