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
