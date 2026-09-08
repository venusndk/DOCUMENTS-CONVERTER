"""
Tests for the /api/v1/pdf/* endpoints (documents_converter/api/app.py,
pdf_utilities.py) -- Phase 11, master directive numbering (PDF
Utilities -- not this project's own, earlier, differently-numbered
Phase 11; see README's disambiguation note).
"""

from __future__ import annotations

import io
import zipfile

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api.app import _rate_limiter, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture: _rate_limiter is a
    module-level singleton shared by every request against this shared
    TestClient "IP", so without resetting it between tests, the many
    requests this file makes would trip the limit partway through."""
    _rate_limiter._counts.clear()
    yield


def _build_pdf_bytes(page_labels: list[str], width=300, height=200) -> bytes:
    doc = fitz.open()
    for label in page_labels:
        page = doc.new_page(width=width, height=height)
        page.insert_text((30, 100), label)
    data = doc.tobytes()
    doc.close()
    return data


def _page_texts_from_bytes(data: bytes) -> list[str]:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return [p.get_text().strip() for p in doc]
    finally:
        doc.close()


def _pdf_file(data: bytes, name="doc.pdf"):
    return {"file": (name, io.BytesIO(data), "application/pdf")}


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


def test_merge_concatenates_uploaded_files_in_order():
    a = _build_pdf_bytes(["A1", "A2"])
    b = _build_pdf_bytes(["B1"])
    resp = client.post(
        "/api/v1/pdf/merge",
        files=[
            ("files", ("a.pdf", io.BytesIO(a), "application/pdf")),
            ("files", ("b.pdf", io.BytesIO(b), "application/pdf")),
        ],
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert _page_texts_from_bytes(resp.content) == ["A1", "A2", "B1"]


def test_merge_rejects_fewer_than_two_files():
    a = _build_pdf_bytes(["A1"])
    resp = client.post(
        "/api/v1/pdf/merge", files=[("files", ("a.pdf", io.BytesIO(a), "application/pdf"))]
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# split / extract / reorder / delete-pages
# --------------------------------------------------------------------------


def test_split_default_is_one_file_per_page():
    data = _build_pdf_bytes(["P1", "P2", "P3"])
    resp = client.post("/api/v1/pdf/split", files=_pdf_file(data))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert len(names) == 3
        assert _page_texts_from_bytes(zf.read(names[0])) == ["P1"]


def test_split_with_explicit_ranges():
    data = _build_pdf_bytes(["P1", "P2", "P3", "P4"])
    resp = client.post(
        "/api/v1/pdf/split", data={"ranges": "1-2;3-4"}, files=_pdf_file(data)
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert len(names) == 2
        assert _page_texts_from_bytes(zf.read(names[0])) == ["P1", "P2"]
        assert _page_texts_from_bytes(zf.read(names[1])) == ["P3", "P4"]


def test_extract_selects_and_reorders():
    data = _build_pdf_bytes(["P1", "P2", "P3"])
    resp = client.post("/api/v1/pdf/extract", data={"pages": "3,1"}, files=_pdf_file(data))
    assert resp.status_code == 200
    assert _page_texts_from_bytes(resp.content) == ["P3", "P1"]


def test_extract_rejects_an_out_of_range_page():
    data = _build_pdf_bytes(["P1"])
    resp = client.post("/api/v1/pdf/extract", data={"pages": "99"}, files=_pdf_file(data))
    assert resp.status_code == 400


def test_reorder_permutes_every_page():
    data = _build_pdf_bytes(["P1", "P2", "P3"])
    resp = client.post("/api/v1/pdf/reorder", data={"order": "3,1,2"}, files=_pdf_file(data))
    assert resp.status_code == 200
    assert _page_texts_from_bytes(resp.content) == ["P3", "P1", "P2"]


def test_reorder_rejects_a_non_permutation():
    data = _build_pdf_bytes(["P1", "P2", "P3"])
    resp = client.post("/api/v1/pdf/reorder", data={"order": "1,2"}, files=_pdf_file(data))
    assert resp.status_code == 400


def test_delete_pages_removes_the_given_pages():
    data = _build_pdf_bytes(["P1", "P2", "P3"])
    resp = client.post("/api/v1/pdf/delete-pages", data={"pages": "2"}, files=_pdf_file(data))
    assert resp.status_code == 200
    assert _page_texts_from_bytes(resp.content) == ["P1", "P3"]


# --------------------------------------------------------------------------
# rotate / watermark / add-page-numbers / crop / compress / repair
# --------------------------------------------------------------------------


def test_rotate_all_pages():
    data = _build_pdf_bytes(["P1", "P2"])
    resp = client.post("/api/v1/pdf/rotate", data={"degrees": 90}, files=_pdf_file(data))
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        assert [p.rotation for p in doc] == [90, 90]
    finally:
        doc.close()


def test_rotate_specific_pages_only():
    data = _build_pdf_bytes(["P1", "P2"])
    resp = client.post(
        "/api/v1/pdf/rotate", data={"degrees": 180, "pages": "2"}, files=_pdf_file(data)
    )
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        assert [p.rotation for p in doc] == [0, 180]
    finally:
        doc.close()


def test_rotate_rejects_a_non_multiple_of_90():
    data = _build_pdf_bytes(["P1"])
    resp = client.post("/api/v1/pdf/rotate", data={"degrees": 45}, files=_pdf_file(data))
    assert resp.status_code == 400


def test_watermark_adds_visible_text():
    data = _build_pdf_bytes(["ORIGINAL"])
    resp = client.post(
        "/api/v1/pdf/watermark", data={"text": "CONFIDENTIAL"}, files=_pdf_file(data)
    )
    assert resp.status_code == 200
    text = _page_texts_from_bytes(resp.content)[0]
    assert "ORIGINAL" in text
    assert "CONFIDENTIAL" in text


def test_add_page_numbers_default_start():
    data = _build_pdf_bytes(["P1", "P2"])
    resp = client.post("/api/v1/pdf/add-page-numbers", files=_pdf_file(data))
    assert resp.status_code == 200
    texts = _page_texts_from_bytes(resp.content)
    assert "1" in texts[0]
    assert "2" in texts[1]


def test_add_page_numbers_custom_start():
    data = _build_pdf_bytes(["P1"])
    resp = client.post(
        "/api/v1/pdf/add-page-numbers", data={"start": 10}, files=_pdf_file(data)
    )
    assert resp.status_code == 200
    assert "10" in _page_texts_from_bytes(resp.content)[0]


def test_crop_shrinks_the_page():
    data = _build_pdf_bytes(["P1"], width=400, height=300)
    resp = client.post(
        "/api/v1/pdf/crop",
        data={"left": 50, "top": 50, "right": 50, "bottom": 50},
        files=_pdf_file(data),
    )
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        rect = doc[0].rect
        assert rect.width == 300
        assert rect.height == 200
    finally:
        doc.close()


def test_crop_rejects_margins_that_consume_the_page():
    data = _build_pdf_bytes(["P1"], width=100, height=100)
    resp = client.post(
        "/api/v1/pdf/crop",
        data={"left": 60, "top": 60, "right": 60, "bottom": 60},
        files=_pdf_file(data),
    )
    assert resp.status_code == 400


def test_compress_returns_a_valid_pdf_with_the_same_content():
    data = _build_pdf_bytes(["P1", "P2"])
    resp = client.post("/api/v1/pdf/compress", files=_pdf_file(data))
    assert resp.status_code == 200
    assert _page_texts_from_bytes(resp.content) == ["P1", "P2"]


def test_repair_returns_a_valid_pdf_with_the_same_content():
    data = _build_pdf_bytes(["P1"])
    resp = client.post("/api/v1/pdf/repair", files=_pdf_file(data))
    assert resp.status_code == 200
    assert _page_texts_from_bytes(resp.content) == ["P1"]


# --------------------------------------------------------------------------
# Cross-cutting: auth, rejection of non-PDF input.
# --------------------------------------------------------------------------


def test_pdf_endpoints_reject_a_non_pdf_file():
    resp = client.post(
        "/api/v1/pdf/rotate",
        data={"degrees": 90},
        files={"file": ("not_a_pdf.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 400


def test_pdf_endpoint_requires_auth_when_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    data = _build_pdf_bytes(["P1"])
    resp = client.post("/api/v1/pdf/repair", files=_pdf_file(data))
    assert resp.status_code == 401
