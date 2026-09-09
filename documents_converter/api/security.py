"""
File-content validation, independent of the client-supplied filename or
Content-Type header -- neither can be trusted (docs/PHASE_0_AUDIT.md
Risk Register / File Security, directive section 28). Two distinct checks:

1. Magic-byte signature: does the file's actual content match a known
   signature for the format its extension claims? Catches a file that's
   been renamed to look like something it isn't.
2. Decompression-bomb guard: a tiny file can still decompress to a huge
   bitmap (image) or an enormous page count (PDF) and exhaust memory/CPU
   well before OCR even starts. Checked on the decompressed/parsed
   result, not the file's on-disk size.
"""

from __future__ import annotations

from pathlib import Path

MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".bmp": (b"BM",),
    # .webp is deliberately NOT here -- see matches_magic_bytes below, it
    # needs a non-prefix check this dict's shape can't express.

    # Phase 9 (master directive numbering): the OOXML Office formats
    # (.docx/.xlsx/.pptx) are all plain ZIP containers -- this signature
    # can only confirm "this is a zip file", not which of the three it
    # actually is (that would need parsing the zip's internal
    # [Content_Types].xml). Real, disclosed limitation, not an oversight:
    # still catches the actual attack this check exists for (a renamed
    # .txt/.exe claiming to be a .docx), just not a .docx renamed to .xlsx.
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    # Legacy binary Office formats (OLE/CFB container) -- same disclosed
    # limitation: one shared signature can't distinguish .doc from .xls
    # from .ppt, only "this is some CFB-container file".
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    # .html/.htm/.md/.markdown are deliberately NOT here -- plain text
    # formats have no reliable magic bytes at all (valid HTML can start
    # with whitespace, a comment, a doctype in any case, etc.; Markdown
    # is unstructured text by design). matches_magic_bytes's existing
    # fallback (no registered signature => accept) applies to these.
}

# Longest signature above is 8 bytes; also enough headroom for the WEBP
# check below (needs bytes 0-11).
MAGIC_BYTES_TO_READ = 16

MAX_IMAGE_MEGAPIXELS = 50
MAX_PDF_PAGES = 200


def matches_magic_bytes(ext: str, header: bytes) -> bool:
    """
    :param ext: lowercased extension including the dot, e.g. ".pdf"
    :param header: the first MAGIC_BYTES_TO_READ bytes of the file
    :return: True if the content matches a known signature for `ext`, or
        if `ext` has no registered signature (callers should reject an
        unsupported extension -- e.g. via the capability registry,
        documents_converter/registry.py -- before acting on the file
        this validates)
    """
    if ext == ".webp":
        # WEBP's container (RIFF) has a 4-byte little-endian file size
        # between "RIFF" and "WEBP" that varies per file, so -- unlike
        # every other format here -- it can't be one fixed byte-string
        # prefix in MAGIC_SIGNATURES. Verified against a real file:
        # Pillow's own WEBP writer produces exactly b"RIFF" + 4 size
        # bytes + b"WEBP" (Phase 8 completion, master directive numbering).
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    sigs = MAGIC_SIGNATURES.get(ext)
    if not sigs:
        return True
    return any(header.startswith(sig) for sig in sigs)


class FileTooLargeError(ValueError):
    """Raised by the decompression-bomb checks below; callers map this to
    an HTTP 413, same as the raw-byte-size check in app.py."""


def check_image_dimensions(width: int, height: int) -> None:
    megapixels = (width * height) / 1_000_000
    if megapixels > MAX_IMAGE_MEGAPIXELS:
        raise FileTooLargeError(
            f"Image is {width}x{height} ({megapixels:.0f} MP), "
            f"over the {MAX_IMAGE_MEGAPIXELS} MP limit."
        )


def check_pdf_page_count(n_pages: int) -> None:
    if n_pages > MAX_PDF_PAGES:
        raise FileTooLargeError(f"PDF has {n_pages} pages, over the {MAX_PDF_PAGES} page limit.")


#: Raster formats check_decompression_bomb below actually opens and
#: measures. Shared with app.py's /api/v1/analyze (which needs the same
#: set to decide whether it can inspect a given upload at all).
IMAGE_BOMB_CHECK_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"})


def check_decompression_bomb(input_path: Path, ext: str) -> None:
    """
    Checks the parsed/decompressed size (PDF page count, image pixel
    dimensions) before the expensive OCR/conversion pipeline runs.
    Raises FileTooLargeError if it's over the configured limit.

    Lives here (not in app.py, where it originated) as of Phase 14
    (master directive numbering): documents_converter/api/rq_tasks.py's
    RQ-executed task needs this exact check too, and must not import
    app.py (a worker process has no reason to construct the FastAPI app
    at all) -- so this is the one shared place both app.py and
    rq_tasks.py import it from, instead of two copies drifting apart.

    Phase 9 (master directive numbering) added Office documents (.docx/
    .xlsx/.pptx and their legacy binary equivalents), HTML, and Markdown
    as acceptable inputs -- none of them get a decompression check here.
    HTML/Markdown are plain text with nothing to decompress. Office
    documents (ZIP containers, like every OOXML format) genuinely CAN be
    zip-bombed, same class of risk as any ZIP-based format -- not
    covered by a check here yet. Real, disclosed gap, not silently
    assumed safe: the raw upload size limit (config.MAX_UPLOAD_MB) and
    the overall conversion timeout (config.CONVERT_TIMEOUT_SECONDS,
    which every capability's call already runs under) are the only
    protection today. Dedicated malicious-file/zip-bomb testing for
    these formats is master directive Phase 19's job (Performance &
    Security Hardening), not this one's.
    """
    if ext == ".pdf":
        import fitz

        doc = fitz.open(str(input_path))
        n_pages = len(doc)
        doc.close()
        check_pdf_page_count(n_pages)
    elif ext in IMAGE_BOMB_CHECK_EXTENSIONS:
        from PIL import Image as PILImage

        with PILImage.open(input_path) as img:
            check_image_dimensions(*img.size)
