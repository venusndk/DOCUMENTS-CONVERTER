"""
Tests for the /api/v1/pdf/{protect,unlock,redact,sign,verify-signatures}
endpoints (documents_converter/api/app.py, pdf_security.py) -- Phase 12,
master directive numbering (Document Security).
"""

from __future__ import annotations

import io

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api.app import _rate_limiter, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _build_pdf_bytes(text="Hello", width=300, height=200) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((30, 100), text)
    data = doc.tobytes()
    doc.close()
    return data


def _pdf_file(data: bytes, name="doc.pdf"):
    return {"file": (name, io.BytesIO(data), "application/pdf")}


# --------------------------------------------------------------------------
# protect / unlock
# --------------------------------------------------------------------------


def test_protect_sets_a_user_password():
    data = _build_pdf_bytes()
    resp = client.post(
        "/api/v1/pdf/protect", data={"user_password": "secret123"}, files=_pdf_file(data)
    )
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        assert doc.is_encrypted
        assert doc.authenticate("wrong") == 0
        assert doc.authenticate("secret123") != 0
    finally:
        doc.close()


def test_protect_rejects_no_password():
    data = _build_pdf_bytes()
    resp = client.post("/api/v1/pdf/protect", files=_pdf_file(data))
    assert resp.status_code == 400


def test_protect_with_permissions_restricts_modify():
    data = _build_pdf_bytes()
    resp = client.post(
        "/api/v1/pdf/protect",
        data={"user_password": "u", "owner_password": "o", "permissions": "print"},
        files=_pdf_file(data),
    )
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        doc.authenticate("u")
        assert doc.permissions & fitz.PDF_PERM_PRINT
        assert not (doc.permissions & fitz.PDF_PERM_MODIFY)
    finally:
        doc.close()


def test_protect_rejects_an_unknown_permission_name():
    data = _build_pdf_bytes()
    resp = client.post(
        "/api/v1/pdf/protect",
        data={"user_password": "u", "permissions": "levitate"},
        files=_pdf_file(data),
    )
    assert resp.status_code == 400


def test_unlock_round_trips_with_the_correct_password():
    data = _build_pdf_bytes()
    protect_resp = client.post(
        "/api/v1/pdf/protect", data={"user_password": "u123"}, files=_pdf_file(data)
    )
    assert protect_resp.status_code == 200

    unlock_resp = client.post(
        "/api/v1/pdf/unlock",
        data={"password": "u123"},
        files=_pdf_file(protect_resp.content, name="protected.pdf"),
    )
    assert unlock_resp.status_code == 200
    doc = fitz.open(stream=unlock_resp.content, filetype="pdf")
    try:
        assert not doc.is_encrypted
    finally:
        doc.close()


def test_unlock_rejects_wrong_password():
    data = _build_pdf_bytes()
    protect_resp = client.post(
        "/api/v1/pdf/protect", data={"user_password": "u123"}, files=_pdf_file(data)
    )
    resp = client.post(
        "/api/v1/pdf/unlock",
        data={"password": "wrong"},
        files=_pdf_file(protect_resp.content, name="protected.pdf"),
    )
    assert resp.status_code == 400


def test_unlock_rejects_an_unprotected_pdf():
    data = _build_pdf_bytes()
    resp = client.post("/api/v1/pdf/unlock", data={"password": "anything"}, files=_pdf_file(data))
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# redact
# --------------------------------------------------------------------------


def test_redact_by_terms_removes_matched_text():
    data = _build_pdf_bytes(text="Name: John Secret Doe")
    resp = client.post("/api/v1/pdf/redact", data={"terms": "Secret"}, files=_pdf_file(data))
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        text = doc[0].get_text()
        assert "Secret" not in text
        assert "John" in text
    finally:
        doc.close()


def test_redact_by_rect_removes_content_in_region():
    data = _build_pdf_bytes(text="Secret Data")
    resp = client.post(
        "/api/v1/pdf/redact", data={"rects": "1,0,80,300,120"}, files=_pdf_file(data)
    )
    assert resp.status_code == 200
    doc = fitz.open(stream=resp.content, filetype="pdf")
    try:
        assert "Secret" not in doc[0].get_text()
    finally:
        doc.close()


def test_redact_requires_terms_or_rects():
    data = _build_pdf_bytes()
    resp = client.post("/api/v1/pdf/redact", files=_pdf_file(data))
    assert resp.status_code == 400


def test_redact_rejects_a_term_that_matches_nothing():
    data = _build_pdf_bytes(text="Nothing sensitive")
    resp = client.post(
        "/api/v1/pdf/redact", data={"terms": "NoSuchTerm"}, files=_pdf_file(data)
    )
    assert resp.status_code == 400


def test_redact_rejects_a_malformed_rect_spec():
    data = _build_pdf_bytes()
    resp = client.post(
        "/api/v1/pdf/redact", data={"rects": "not-a-valid-rect"}, files=_pdf_file(data)
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# sign / verify-signatures
# --------------------------------------------------------------------------


def test_sign_then_verify_reports_intact_and_untrusted():
    data = _build_pdf_bytes()
    sign_resp = client.post(
        "/api/v1/pdf/sign",
        data={"reason": "Approval", "location": "API test"},
        files=_pdf_file(data),
    )
    assert sign_resp.status_code == 200

    verify_resp = client.post(
        "/api/v1/pdf/verify-signatures",
        files=_pdf_file(sign_resp.content, name="signed.pdf"),
    )
    assert verify_resp.status_code == 200
    signatures = verify_resp.json()["signatures"]
    assert len(signatures) == 1
    sig = signatures[0]
    assert sig["signer_cn"] == "Documents Converter Demo Signer"
    assert sig["reason"] == "Approval"
    assert sig["location"] == "API test"
    assert sig["intact"] is True
    assert sig["trusted"] is False
    assert sig["modified_after_signing"] is False


def test_verify_signatures_on_an_unsigned_pdf_returns_empty_list():
    data = _build_pdf_bytes()
    resp = client.post("/api/v1/pdf/verify-signatures", files=_pdf_file(data))
    assert resp.status_code == 200
    assert resp.json()["signatures"] == []


# --------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------


def test_security_endpoints_reject_a_non_pdf_file():
    resp = client.post(
        "/api/v1/pdf/redact",
        data={"terms": "x"},
        files={"file": ("not_a_pdf.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 400


def test_security_endpoint_requires_auth_when_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    data = _build_pdf_bytes()
    resp = client.post("/api/v1/pdf/verify-signatures", files=_pdf_file(data))
    assert resp.status_code == 401
