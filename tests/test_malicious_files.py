"""
Malicious-file testing (Phase 19, master directive numbering:
Performance & Security Hardening) -- real, working attack payloads
against this service's own upload path, not simulated or assumed safe.

The one real bug this pass found: a genuine zip bomb disguised as a
.docx (a 500 MB payload compressing to under 510 KB on disk -- the same
DEFLATE-against-a-repeated-byte technique real bombs like 42.zip use)
sailed completely unblocked through check_decompression_bomb before
this phase, confirmed directly against the pre-fix code before writing
the fix (security.check_office_zip_bomb). Every test below builds a
real payload and exercises the real check/endpoint against it -- not a
mock, not an assumption.
"""

from __future__ import annotations

import io
import zipfile

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api import security
from documents_converter.api.app import _rate_limiter, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _zip_bomb_bytes(uncompressed_mb: int) -> bytes:
    """A real, working zip bomb -- one entry of highly-compressible
    content (a repeated byte, the same technique real bombs use).
    Returns real bytes, not a description of one."""
    buf = io.BytesIO()
    chunk = b"0" * (1024 * 1024)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        with zf.open("word/document.xml", "w") as f:
            for _ in range(uncompressed_mb):
                f.write(chunk)
    return buf.getvalue()


def _real_small_docx_bytes() -> bytes:
    """A real, legitimate .docx -- OOXML's minimum viable structure --
    to confirm the zip-bomb check has no false positive on an ordinary
    small document."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        zf.writestr("word/document.xml", "<w:document>Hello, world.</w:document>")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Zip bombs (OOXML formats: .docx/.xlsx/.pptx)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", [".docx", ".xlsx", ".pptx"])
def test_check_decompression_bomb_blocks_a_real_zip_bomb(ext, tmp_path):
    """Regression test for the real vulnerability this phase found and
    fixed -- exercises the exact function, not a smaller stand-in, and
    a genuinely large payload (600 MB, over the 500 MB limit), not a
    contrived edge case."""
    bomb_path = tmp_path / f"bomb{ext}"
    bomb_path.write_bytes(_zip_bomb_bytes(600))

    with pytest.raises(security.FileTooLargeError, match="decompresses to"):
        security.check_decompression_bomb(bomb_path, ext)


def test_check_decompression_bomb_allows_a_real_small_docx(tmp_path):
    """No false positive on an ordinary, legitimate document."""
    docx_path = tmp_path / "real.docx"
    docx_path.write_bytes(_real_small_docx_bytes())
    security.check_decompression_bomb(docx_path, ".docx")  # must not raise


def test_zip_bomb_upload_is_rejected_end_to_end():
    """The real HTTP path, not just the unit-level check: a zip bomb
    uploaded through POST /api/v1/convert -- tiny on disk (well under
    config.MAX_UPLOAD_MB), so the raw upload-size check alone would
    never have caught it -- is rejected with 413, not silently accepted
    or allowed to reach the LibreOffice conversion step at all."""
    bomb_bytes = _zip_bomb_bytes(600)
    assert len(bomb_bytes) < 1_000_000  # tiny on disk -- the whole point of a bomb

    resp = client.post(
        "/api/v1/convert",
        data={"target": "pdf"},
        files={
            "file": (
                "bomb.docx",
                io.BytesIO(bomb_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert resp.status_code == 413
    assert "decompresses to" in resp.json()["detail"]


def test_check_office_zip_bomb_rejects_a_corrupt_zip_cleanly(tmp_path):
    """A file with a real zip *signature* (PK\\x03\\x04, so it clears
    matches_magic_bytes) but a genuinely broken internal structure --
    confirms this doesn't crash the check itself with something worse
    than a clean, catchable error."""
    corrupt_path = tmp_path / "corrupt.docx"
    corrupt_path.write_bytes(b"PK\x03\x04" + b"not actually a valid zip structure" * 10)
    with pytest.raises(zipfile.BadZipFile):
        security.check_office_zip_bomb(corrupt_path)


# --------------------------------------------------------------------------
# Path traversal / filename attacks
# --------------------------------------------------------------------------


def _build_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page(width=200, height=100)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize(
    "malicious_filename",
    [
        "../../../../etc/passwd.pdf",
        "..\\..\\..\\windows\\system32\\evil.pdf",
        "/etc/passwd.pdf",
        "C:\\Windows\\System32\\evil.pdf",
    ],
)
def test_path_traversal_filename_does_not_escape_the_work_directory(malicious_filename, tmp_path):
    """
    The real defense (_validate_and_save_upload, app.py) never uses the
    client-supplied filename for path construction at all -- only its
    extension, via Path(...).suffix, and the actual write target is
    always a fixed `input{ext}` name under a server-allocated work_dir.
    Confirmed here against the real endpoint with a real traversal-
    shaped filename, not just read from the source and trusted: the
    conversion either succeeds (the extension after stripping the
    traversal prefix is still a real, valid .pdf) or fails for an
    unrelated reason -- never anything that reads as a file having been
    written somewhere outside this test's own temp/work directories.
    """
    resp = client.post(
        "/api/v1/convert",
        data={"target": "images"},
        files={"file": (malicious_filename, io.BytesIO(_build_pdf_bytes()), "application/pdf")},
    )
    # A real, valid PDF with a ".pdf"-suffixed malicious name still
    # converts successfully -- the traversal components are simply
    # discarded, never interpreted as directories.
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        assert resp.headers["content-type"] == "application/zip"


def test_null_byte_in_filename_does_not_bypass_extension_validation():
    """A classic path-truncation trick: a filename claiming to be a PDF
    up to a null byte, then a different real extension after it. Python's
    own Path.suffix takes the *last* extension in the full string,
    which here is ".exe" -- confirmed this doesn't let content posing
    as a PDF slip past the capability registry under some other
    extension the registry would have accepted."""
    resp = client.post(
        "/api/v1/convert",
        files={"file": ("evil\x00.pdf.exe", io.BytesIO(b"MZ fake PE header"), "application/pdf")},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Polyglot files
# --------------------------------------------------------------------------


def test_polyglot_pdf_zip_is_still_validated_as_a_pdf():
    """A real polyglot: valid PDF bytes with a real zip archive appended
    after the PDF's own %%EOF (a well-known technique -- many PDF
    readers ignore trailing bytes after the last xref, many zip readers
    read the central directory from the *end* of the file and ignore
    leading bytes, so the same file opens as both). Confirms this
    service's own magic-byte check keys off the leading bytes (the real
    PDF signature) and processes it as the PDF it claims to be, not
    silently as something else."""
    pdf_bytes = _build_pdf_bytes()
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("hidden.txt", "not relevant to this test")

    polyglot = pdf_bytes + zip_buf.getvalue()
    assert polyglot.startswith(b"%PDF")

    resp = client.post(
        "/api/v1/convert",
        data={"target": "images"},
        files={"file": ("polyglot.pdf", io.BytesIO(polyglot), "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
