"""
Tests for PDF security (documents_converter/pdf_security.py) -- Phase
12, master directive numbering (Document Security: password protection,
encryption, redaction, signing, permissions).
"""

from __future__ import annotations

import fitz
import pytest

from documents_converter import pdf_security as ps


def _build_pdf(path, text="Hello") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 100), text)
    doc.save(str(path))
    doc.close()


# --------------------------------------------------------------------------
# parse_permissions
# --------------------------------------------------------------------------


def test_parse_permissions_combines_bits():
    mask = ps.parse_permissions(["print", "copy"])
    assert mask & fitz.PDF_PERM_PRINT
    assert mask & fitz.PDF_PERM_COPY
    assert not (mask & fitz.PDF_PERM_MODIFY)


def test_parse_permissions_is_case_insensitive():
    assert ps.parse_permissions(["PRINT"]) == ps.parse_permissions(["print"])


def test_parse_permissions_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown permission"):
        ps.parse_permissions(["telekinesis"])


# --------------------------------------------------------------------------
# protect_pdf / remove_protection
# --------------------------------------------------------------------------


def test_protect_pdf_requires_at_least_one_password(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    with pytest.raises(ValueError, match="At least one"):
        ps.protect_pdf(src, tmp_path / "out.pdf")


def test_protect_pdf_sets_a_user_password(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    output_path = tmp_path / "out.pdf"

    ps.protect_pdf(src, output_path, user_password="secret123")

    doc = fitz.open(str(output_path))
    try:
        assert doc.is_encrypted
        assert doc.authenticate("wrong-password") == 0
        assert doc.authenticate("secret123") != 0
    finally:
        doc.close()


def test_protect_pdf_enforces_granted_permissions_only(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    output_path = tmp_path / "out.pdf"

    ps.protect_pdf(
        src,
        output_path,
        user_password="u",
        owner_password="o",
        permissions=ps.parse_permissions(["print"]),
    )

    doc = fitz.open(str(output_path))
    try:
        doc.authenticate("u")
        assert doc.permissions & fitz.PDF_PERM_PRINT
        assert not (doc.permissions & fitz.PDF_PERM_MODIFY)
        assert not (doc.permissions & fitz.PDF_PERM_COPY)
    finally:
        doc.close()


def test_protect_pdf_owner_only_leaves_the_file_openable(tmp_path):
    """Owner-password-only encryption is a real use case (anyone can
    view, restrictions apply beyond that) -- must not require a user
    password to open."""
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    output_path = tmp_path / "out.pdf"

    ps.protect_pdf(src, output_path, owner_password="o123", permissions=0)

    doc = fitz.open(str(output_path))
    try:
        assert not doc.needs_pass
    finally:
        doc.close()


def test_remove_protection_with_correct_password(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    protected = tmp_path / "protected.pdf"
    ps.protect_pdf(src, protected, user_password="u123")

    output_path = tmp_path / "out.pdf"
    ps.remove_protection(protected, output_path, "u123")

    doc = fitz.open(str(output_path))
    try:
        assert not doc.is_encrypted
    finally:
        doc.close()


def test_remove_protection_rejects_wrong_password(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    protected = tmp_path / "protected.pdf"
    ps.protect_pdf(src, protected, user_password="u123")

    with pytest.raises(ValueError, match="Incorrect password"):
        ps.remove_protection(protected, tmp_path / "out.pdf", "wrong")


def test_remove_protection_rejects_an_unprotected_pdf(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    with pytest.raises(ValueError, match="not password-protected"):
        ps.remove_protection(src, tmp_path / "out.pdf", "anything")


# --------------------------------------------------------------------------
# redact_pdf
# --------------------------------------------------------------------------


def test_redact_pdf_requires_terms_or_rects(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    with pytest.raises(ValueError, match="At least one"):
        ps.redact_pdf(src, tmp_path / "out.pdf")


def test_redact_pdf_by_search_term_permanently_removes_text(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, text="Name: John Secret Doe")
    output_path = tmp_path / "out.pdf"

    count = ps.redact_pdf(src, output_path, terms=["Secret"])

    assert count == 1
    doc = fitz.open(str(output_path))
    try:
        text = doc[0].get_text()
        assert "Secret" not in text
        assert "John" in text
        assert "Doe" in text
    finally:
        doc.close()


def test_redact_pdf_raises_when_the_term_is_not_found(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, text="Nothing sensitive here")
    with pytest.raises(ValueError, match="No matches found"):
        ps.redact_pdf(src, tmp_path / "out.pdf", terms=["NoSuchTerm"])


def test_redact_pdf_by_explicit_rect(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src, text="Secret Data")
    output_path = tmp_path / "out.pdf"

    count = ps.redact_pdf(src, output_path, rects=[(0, 0, 80, 300, 120)])

    assert count == 1
    doc = fitz.open(str(output_path))
    try:
        assert "Secret" not in doc[0].get_text()
    finally:
        doc.close()


def test_redact_pdf_rejects_an_out_of_range_page(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    with pytest.raises(ValueError, match="out of range"):
        ps.redact_pdf(src, tmp_path / "out.pdf", rects=[(5, 0, 0, 10, 10)])


# --------------------------------------------------------------------------
# sign_pdf / verify_pdf
# --------------------------------------------------------------------------


def test_sign_and_verify_a_pdf_with_the_demo_signer(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    signed = tmp_path / "signed.pdf"

    signer = ps.generate_demo_signer()
    ps.sign_pdf(src, signed, signer, reason="Approval", location="Test suite")

    results = ps.verify_pdf(signed)

    assert len(results) == 1
    result = results[0]
    assert result["field_name"] == "Signature1"
    assert result["signer_cn"] == "Documents Converter Demo Signer"
    assert result["reason"] == "Approval"
    assert result["location"] == "Test suite"
    assert result["intact"] is True
    # The demo certificate is self-signed on purpose (see
    # generate_demo_signer's docstring) -- it must never report as
    # trusted, since that would be a false claim about identity.
    assert result["trusted"] is False
    assert result["modified_after_signing"] is False


def test_verify_pdf_returns_empty_list_for_an_unsigned_pdf(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    assert ps.verify_pdf(src) == []


def test_verify_pdf_detects_tampering_after_signing(tmp_path):
    src = tmp_path / "src.pdf"
    _build_pdf(src)
    signed = tmp_path / "signed.pdf"
    ps.sign_pdf(src, signed, ps.generate_demo_signer())

    # Confirm the untampered baseline first -- otherwise a
    # modified_after_signing that's *always* True (a broken check) would
    # pass this test just as easily as a correct one.
    assert ps.verify_pdf(signed)[0]["modified_after_signing"] is False

    # Tamper with the signed file's visible content via a real,
    # otherwise-legitimate PDF mechanism (an incremental update) --
    # exactly the kind of change a naive "did the file change size"
    # check would miss, but a real signature-diff analysis catches.
    # Deliberately NOT asserted via `bottom_line`: that field is already
    # False before this tampering too (the demo cert is untrusted by
    # design), so it can't distinguish "untrusted" from "untrusted AND
    # tampered" -- modified_after_signing exists specifically to carry
    # that distinction; see verify_pdf's docstring.
    doc = fitz.open(str(signed))
    doc[0].insert_text((30, 200), "TAMPERED")
    doc.saveIncr()
    doc.close()

    result = ps.verify_pdf(signed)[0]
    assert result["modified_after_signing"] is True
    assert result["bottom_line"] is False


def test_generate_demo_signer_is_cached_across_calls():
    assert ps.generate_demo_signer() is ps.generate_demo_signer()


def test_load_pkcs12_signer_rejects_garbage_data():
    with pytest.raises(ValueError, match="Could not load"):
        ps.load_pkcs12_signer(b"not a real pkcs12 file")
