"""
CellOCRProvider -- recognizes text in one already-cropped cell image.

This is a distinct, narrower job than img2table's own OCR pass (which reads
a whole page and segments it into words/lines itself): the two call sites
that need this -- rotated-header correction and the grid-line-detection
fallback -- have already isolated a single cell's pixels and just need
"what text is in this crop", optionally after rotating it back to
horizontal first. Wrapping that one operation behind an interface means a
different engine (a cloud OCR API for a handful of low-confidence cells,
say) could be substituted later without touching the cropping/rotation
logic that calls it.
"""

from __future__ import annotations

from typing import Protocol

import cv2
import pytesseract


class CellOCRProvider(Protocol):
    def recognize(self, gray_crop, *, rotate: bool = False) -> str | None:
        """
        :param gray_crop: single-channel (grayscale) numpy image of one cell
        :param rotate: rotate 90 degrees clockwise before recognizing --
            for cells whose text is printed sideways
        :return: recognized text, or None if the crop is empty or nothing
            was recognized
        """
        ...


class TesseractCellOCR:
    """The concrete implementation used throughout this project so far:
    upscale (Tesseract reads small crops poorly at native size) then run
    Tesseract with a page-segmentation mode suited to a short isolated
    block of text rather than a full-page layout.
    """

    def __init__(self, upscale: int = 3, psm: int = 6):
        self.upscale = upscale
        self.psm = psm

    def recognize(self, gray_crop, *, rotate: bool = False) -> str | None:
        if gray_crop.size == 0:
            return None
        if rotate:
            gray_crop = cv2.rotate(gray_crop, cv2.ROTATE_90_CLOCKWISE)
        upscaled = cv2.resize(
            gray_crop, None, fx=self.upscale, fy=self.upscale, interpolation=cv2.INTER_CUBIC
        )
        text = pytesseract.image_to_string(upscaled, config=f"--psm {self.psm}").strip()
        return text or None
