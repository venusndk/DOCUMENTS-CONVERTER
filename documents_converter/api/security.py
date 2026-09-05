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
}

# Longest signature above is 8 bytes; read a little extra headroom.
MAGIC_BYTES_TO_READ = 16

MAX_IMAGE_MEGAPIXELS = 50
MAX_PDF_PAGES = 200


def matches_magic_bytes(ext: str, header: bytes) -> bool:
    """
    :param ext: lowercased extension including the dot, e.g. ".pdf"
    :param header: the first MAGIC_BYTES_TO_READ bytes of the file
    :return: True if the content matches a known signature for `ext`, or
        if `ext` has no registered signature (callers should already be
        rejecting unknown extensions via an allowlist before this runs)
    """
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
