"""
Tests for the PDF utilities (documents_converter/pdf_utilities.py) --
Phase 11, master directive numbering (PDF Utilities -- not this
project's own, earlier, differently-numbered Phase 11).
"""

from __future__ import annotations

import fitz
import pytest

from documents_converter.pdf_utilities import (
    add_page_numbers,
    add_watermark,
    compress_pdf,
    crop_pages,
    delete_pages,
    extract_pages,
    merge_pdfs,
    parse_page_spec,
    repair_pdf,
    reorder_pages,
    rotate_pages,
    split_pdf,
)


def _build_pdf(path, page_labels: list[str], width=300, height=200) -> None:
    """One page per label, each with the label as its only visible text
    -- lets tests confirm page identity/order after an operation by
    reading each page's text back, not just counting pages."""
    doc = fitz.open()
    for label in page_labels:
        page = doc.new_page(width=width, height=height)
        page.insert_text((30, 100), label)
    doc.save(str(path))
    doc.close()


def _page_texts(path) -> list[str]:
    doc = fitz.open(str(path))
    try:
        return [p.get_text().strip() for p in doc]
    finally:
        doc.close()


# --------------------------------------------------------------------------
# parse_page_spec
# --------------------------------------------------------------------------


def test_parse_page_spec_single_pages():
    assert parse_page_spec("1,3,5", page_count=5) == [0, 2, 4]


def test_parse_page_spec_ranges():
    assert parse_page_spec("1-3", page_count=5) == [0, 1, 2]


def test_parse_page_spec_mixed():
    assert parse_page_spec("1,3-5,2", page_count=5) == [0, 2, 3, 4, 1]


def test_parse_page_spec_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        parse_page_spec("1,99", page_count=5)


def test_parse_page_spec_rejects_backwards_range():
    with pytest.raises(ValueError, match="start must be <= end"):
        parse_page_spec("5-1", page_count=5)


def test_parse_page_spec_rejects_garbage():
    with pytest.raises(ValueError):
        parse_page_spec("abc", page_count=5)


# --------------------------------------------------------------------------
# merge / split / extract / reorder / delete
# --------------------------------------------------------------------------


def test_merge_pdfs_concatenates_in_order(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _build_pdf(a, ["A1", "A2"])
    _build_pdf(b, ["B1"])

    output_path = tmp_path / "merged.pdf"
    merge_pdfs([a, b], output_path)

    assert _page_texts(output_path) == ["A1", "A2", "B1"]


def test_split_pdf_default_one_file_per_page(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    parts = split_pdf(src, out_dir)

    assert len(parts) == 3
    assert [_page_texts(p)[0] for p in parts] == ["P1", "P2", "P3"]


def test_split_pdf_with_explicit_ranges(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3", "P4"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    parts = split_pdf(src, out_dir, ranges=[[0, 1], [2, 3]])

    assert len(parts) == 2
    assert _page_texts(parts[0]) == ["P1", "P2"]
    assert _page_texts(parts[1]) == ["P3", "P4"]


def test_extract_pages_selects_and_can_reorder(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])
    output_path = tmp_path / "out.pdf"

    extract_pages(src, output_path, pages=[2, 0])

    assert _page_texts(output_path) == ["P3", "P1"]


def test_reorder_pages_permutes_every_page(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])
    output_path = tmp_path / "out.pdf"

    reorder_pages(src, output_path, order=[2, 0, 1])

    assert _page_texts(output_path) == ["P3", "P1", "P2"]


def test_reorder_pages_rejects_a_non_permutation(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])

    with pytest.raises(ValueError, match="permutation"):
        reorder_pages(src, tmp_path / "out.pdf", order=[0, 1])


def test_delete_pages_removes_only_the_given_pages(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])
    output_path = tmp_path / "out.pdf"

    delete_pages(src, output_path, pages=[1])

    assert _page_texts(output_path) == ["P1", "P3"]


# --------------------------------------------------------------------------
# rotate / watermark / page numbers / crop / compress / repair
# --------------------------------------------------------------------------


def test_rotate_pages_all_pages(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2"])
    output_path = tmp_path / "out.pdf"

    rotate_pages(src, output_path, degrees=90)

    doc = fitz.open(str(output_path))
    try:
        assert [p.rotation for p in doc] == [90, 90]
    finally:
        doc.close()


def test_rotate_pages_specific_pages_only(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])
    output_path = tmp_path / "out.pdf"

    rotate_pages(src, output_path, degrees=180, pages=[1])

    doc = fitz.open(str(output_path))
    try:
        assert [p.rotation for p in doc] == [0, 180, 0]
    finally:
        doc.close()


def test_rotate_pages_rejects_non_multiple_of_90(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1"])
    with pytest.raises(ValueError, match="multiple of 90"):
        rotate_pages(src, tmp_path / "out.pdf", degrees=45)


def test_add_watermark_adds_visible_text_without_losing_original_content(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["ORIGINAL CONTENT"])
    output_path = tmp_path / "out.pdf"

    add_watermark(src, output_path, text="CONFIDENTIAL")

    text = _page_texts(output_path)[0]
    assert "ORIGINAL CONTENT" in text
    assert "CONFIDENTIAL" in text


def test_add_watermark_shrinks_long_text_to_fit_a_small_page(tmp_path):
    """Specifically exercises _fit_watermark_fontsize's shrink loop
    (a long string on a small page) rather than relying on the previous
    test's text/page-size combination, which fits at max size without
    needing to shrink at all."""
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["X"], width=150, height=100)
    output_path = tmp_path / "out.pdf"

    add_watermark(src, output_path, text="STRICTLY CONFIDENTIAL DO NOT DISTRIBUTE")

    text = _page_texts(output_path)[0]
    assert "STRICTLY CONFIDENTIAL DO NOT DISTRIBUTE" in text


def test_add_page_numbers_stamps_each_page(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2", "P3"])
    output_path = tmp_path / "out.pdf"

    add_page_numbers(src, output_path, start=1)

    for i, text in enumerate(_page_texts(output_path), start=1):
        assert str(i) in text


def test_add_page_numbers_honors_a_custom_start(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2"])
    output_path = tmp_path / "out.pdf"

    add_page_numbers(src, output_path, start=5)

    texts = _page_texts(output_path)
    assert "5" in texts[0]
    assert "6" in texts[1]


def test_crop_pages_shrinks_the_visible_area(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1"], width=400, height=300)
    output_path = tmp_path / "out.pdf"

    crop_pages(src, output_path, margins=(50, 50, 50, 50))

    doc = fitz.open(str(output_path))
    try:
        rect = doc[0].rect
        assert rect.width == 300  # 400 - 50 - 50
        assert rect.height == 200  # 300 - 50 - 50
    finally:
        doc.close()


def test_crop_pages_rejects_margins_that_consume_the_whole_page(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1"], width=100, height=100)
    with pytest.raises(ValueError):
        crop_pages(src, tmp_path / "out.pdf", margins=(60, 60, 60, 60))


def test_compress_pdf_produces_a_valid_pdf_with_same_content(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1", "P2"])
    output_path = tmp_path / "out.pdf"

    compress_pdf(src, output_path)

    assert output_path.exists()
    assert _page_texts(output_path) == ["P1", "P2"]


def test_repair_pdf_resaves_a_valid_pdf_unchanged_in_content(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, ["P1"])
    output_path = tmp_path / "out.pdf"

    repair_pdf(src, output_path)

    assert output_path.exists()
    assert _page_texts(output_path) == ["P1"]
