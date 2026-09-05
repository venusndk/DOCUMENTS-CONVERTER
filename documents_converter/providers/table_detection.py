"""
TableDetector -- finds and extracts tables from a rendered document.

Wraps a three-stage strategy, each stage added after a specific real
failure was found on real documents (see docs/PHASE_0_AUDIT.md):

1. Per-page bordered-vs-borderless mode selection (_pick_extraction_mode):
   neither mode is uniformly better -- one document needed borderless to
   find a page's table at all, a different document got a clean table
   fragmented into broken pieces under borderless when bordered alone
   found it correctly.
2. img2table's own detection, run with whichever mode each page picked.
3. A grid-line-detection fallback (classical CV, bypassing img2table's
   detector and its OCR-based content-refinement step entirely) for any
   page where stage 2 still returns nothing usable, or -- confirmed on a
   real document -- quietly collapses columns during its own refinement
   (20 genuine columns collapsed to 16, with the dropped columns' values
   replaced by leftover header text repeated into every row).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np
from img2table.document import PDF
from img2table.tables.extraction import BBox, ExtractedTable, TableCell
from img2table.tables.extractor import TableExtractor

from .cell_ocr import CellOCRProvider, TesseractCellOCR

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


# img2table's own PDF class rasterizes pages at a fixed ~200 DPI (scale=200/72),
# which is too low for dense/small print and is a major source of OCR errors on
# real-world scans. HighResPDF overrides page rendering to use PyMuPDF directly
# at a configurable DPI (default 200 -- see the dpi field's own note), with
# optional light preprocessing, while reusing all of img2table's table
# detection/extraction/xlsx-writing logic as-is.
@dataclass
class HighResPDF(PDF):
    # NOTE: img2table's own table-border/line detection is tuned around its
    # built-in ~200 DPI rendering. Raising this substantially (e.g. 300+) can
    # make grid lines "too thick" in pixel terms and cause table detection to
    # miss pages entirely -- verified on real documents where 300 DPI found
    # tables on fewer pages than 200 DPI did. 200 is the safe default; only
    # raise it if you've confirmed on your own document that detection still
    # finds the same (or more) tables at the higher setting.
    dpi: int = 200
    preprocess: bool = False

    @property
    def images(self):
        if self._images is None:
            if fitz is None:
                raise RuntimeError("PyMuPDF (fitz) is required for high-DPI PDF rendering.")

            self._ensure_pages()
            assert self.pages is not None

            doc = fitz.open(stream=self.file_bytes, filetype="pdf")
            zoom = self.dpi / 72
            matrix = fitz.Matrix(zoom, zoom)

            images = []
            try:
                for page_number in self.pages:
                    pix = doc[page_number].get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
                    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                        pix.height, pix.width, pix.n
                    )
                    if pix.n == 4:
                        img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)

                    if self.detect_rotation:
                        from img2table.document.rotation import fix_rotation_image
                        img, rotated = fix_rotation_image(img=img)
                        self._rotated = self._rotated or rotated

                    if self.preprocess:
                        img = _preprocess_image(img)

                    images.append(img)
            finally:
                doc.close()

            self._images = images

        return self._images


def _preprocess_image(img):
    """
    Light, table-safe image cleanup to help OCR on noisy/low-contrast scans:
    denoise + contrast normalization (CLAHE). Deliberately avoids hard
    binarization, which tends to erase faint table borders that img2table's
    line-detection relies on.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, h=10)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)


def _pick_extraction_mode(page_img, min_rows: int = 5) -> bool:
    """
    Cheaply (no OCR -- pure shape detection) compares bordered vs borderless
    table detection for one page and returns whether borderless_tables
    should be used. See module docstring point 1.
    :return: True if borderless_tables should be used for this page
    """
    def score(tables):
        good = [t for t in tables if len(t.rows) >= 3]
        if not good:
            return (-1, 0)
        # Prefer more total rows, but also prefer fewer separate tables --
        # a single coherent table splitting into several small fragments
        # (each individually "big enough") is exactly the failure mode
        # being guarded against here.
        return (sum(len(t.rows) for t in good), -len(good))

    bordered = TableExtractor(img=page_img).extract_tables(
        implicit_rows=True, implicit_columns=False, borderless_tables=False
    )
    borderless = TableExtractor(img=page_img).extract_tables(
        implicit_rows=True, implicit_columns=False, borderless_tables=True
    )
    bordered_score = score(bordered)
    borderless_score = score(borderless)

    # Bordered is the stricter, cleaner method when it finds anything
    # plausible at all; only reach for borderless when bordered came up
    # empty/implausible, or borderless is a clear, real improvement.
    if bordered_score[0] >= min_rows and borderless_score <= bordered_score:
        return False
    return borderless_score[0] > bordered_score[0]


def _detect_grid_lines(gray, min_line_frac: float = 0.12, min_gap: int = 10):
    """
    Detects a table's row/column boundaries by direct classical line
    detection (morphological opening with long horizontal/vertical
    structuring elements) rather than relying on img2table's own table
    detector. See module docstring point 3.
    :return: (row_boundary_ys, col_boundary_xs), each sorted ascending;
        empty lists if no plausible grid was found.
    """
    h, w = gray.shape
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 25, 15), 1))
    horizontal = cv2.morphologyEx(bw, cv2.MORPH_OPEN, horiz_kernel, iterations=1)

    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 25, 15)))
    vertical = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vert_kernel, iterations=1)

    h_rowsum = (horizontal > 0).sum(axis=1)
    v_colsum = (vertical > 0).sum(axis=0)

    def cluster_and_filter(indices, min_frac_len):
        if len(indices) == 0:
            return []
        groups = [[int(indices[0])]]
        for i in indices[1:]:
            if i - groups[-1][-1] <= 5:
                groups[-1].append(int(i))
            else:
                groups.append([int(i)])
        positions = [int(np.mean(g)) for g in groups]
        # Drop lines sitting implausibly close to the previous one (noise,
        # not a real second grid line).
        filtered = [positions[0]]
        for p in positions[1:]:
            if p - filtered[-1] >= min_gap:
                filtered.append(p)
        return filtered

    row_lines = cluster_and_filter(np.where(h_rowsum > w * min_line_frac)[0], min_gap)
    col_lines = cluster_and_filter(np.where(v_colsum > h * min_line_frac)[0], min_gap)
    return row_lines, col_lines


def _manual_grid_table(page_img, cell_ocr: CellOCRProvider, min_rows: int = 3, min_cols: int = 3):
    """
    Reconstructs a table for one page via direct grid-line detection plus
    per-cell OCR (with the same rotated-text handling as the rotated-header
    correction step), returning an img2table ExtractedTable so it drops
    into the existing xlsx-writing path unchanged. Returns None if no
    plausible grid is found.
    """
    gray = cv2.cvtColor(page_img, cv2.COLOR_RGB2GRAY)
    img_h, img_w = gray.shape
    row_lines, col_lines = _detect_grid_lines(gray)

    if len(row_lines) < min_rows or len(col_lines) < min_cols:
        return None

    margin = 3
    content = OrderedDict()
    for r in range(len(row_lines) - 1):
        y1, y2 = row_lines[r], row_lines[r + 1]
        row_cells = []
        for c in range(len(col_lines) - 1):
            x1, x2 = col_lines[c], col_lines[c + 1]
            w, h = x2 - x1, y2 - y1

            cx1, cy1 = max(x1 + margin, 0), max(y1 + margin, 0)
            cx2, cy2 = min(x2 - margin, img_w), min(y2 - margin, img_h)
            crop = gray[cy1:cy2, cx1:cx2]

            rotate = w > 0 and h > 0 and h >= 60 and (h / w) >= 1.3
            value = cell_ocr.recognize(crop, rotate=rotate)

            row_cells.append(TableCell(bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2), value=value))
        content[r] = row_cells

    return ExtractedTable(
        bbox=BBox(x1=col_lines[0], y1=row_lines[0], x2=col_lines[-1], y2=row_lines[-1]),
        title=None,
        content=content,
    )


class TableDetector:
    """Orchestrates the three-stage strategy described in the module
    docstring. Constructed once per conversion with the document-wide
    settings; `detect_all` does the actual per-page work.
    """

    MIN_PLAUSIBLE_ROWS = 8
    MIN_COLUMN_RETENTION = 0.85

    def __init__(
        self,
        dpi: int = 200,
        preprocess: bool = False,
        auto_rotate: bool = False,
        auto_detect_mode: bool = True,
        use_grid_fallback: bool = True,
        cell_ocr: CellOCRProvider | None = None,
    ):
        self.dpi = dpi
        self.preprocess = preprocess
        self.auto_rotate = auto_rotate
        self.auto_detect_mode = auto_detect_mode
        self.use_grid_fallback = use_grid_fallback
        self.cell_ocr = cell_ocr or TesseractCellOCR()

    def detect_all(
        self,
        *,
        file_path: str,
        doc,
        file_type: str,
        pages: list[int],
        page_to_image: dict[int, np.ndarray],
        ocr,
        borderless_tables: bool,
        progress: Callable[[str], None] = print,
    ) -> tuple[dict[int, list[ExtractedTable]], set[int]]:
        """
        :return: (extracted_tables, fallback_pages) -- fallback_pages is the
            set of page numbers whose table came from the grid-line fallback
            rather than img2table, so later per-cell rotation correction
            (which the fallback already does internally) can skip them.
        """
        if self.auto_detect_mode and file_type != "image":
            # Grouped into two batched OCR passes rather than one call per
            # page, so this doesn't double the OCR cost of the whole document.
            borderless_pages = [p for p in pages if _pick_extraction_mode(page_to_image[p])]
            bordered_pages = [p for p in pages if p not in borderless_pages]
            if borderless_pages and bordered_pages:
                progress(
                    f"Auto-selected table-detection mode per page: "
                    f"{len(bordered_pages)} page(s) bordered, "
                    f"{len(borderless_pages)} page(s) borderless."
                )

            extracted_tables: dict[int, list[ExtractedTable]] = {}
            for mode, mode_pages in ((False, bordered_pages), (True, borderless_pages)):
                if not mode_pages:
                    continue
                sub_doc = HighResPDF(
                    src=file_path,
                    dpi=self.dpi,
                    preprocess=self.preprocess,
                    detect_rotation=self.auto_rotate,
                    pages=mode_pages,
                    _images=[page_to_image[p] for p in mode_pages],
                )
                extracted_tables.update(
                    sub_doc.extract_tables(
                        ocr=ocr, implicit_rows=True, borderless_tables=mode, min_confidence=50
                    )
                )
        else:
            extracted_tables = doc.extract_tables(
                ocr=ocr,
                implicit_rows=True,
                borderless_tables=borderless_tables,
                min_confidence=50,
            )
            # Both Image (list) and PDF (dict keyed by page number) are
            # supported by img2table's own to_xlsx(); normalize the same way.
            extracted_tables = (
                {0: extracted_tables} if isinstance(extracted_tables, list) else extracted_tables
            )

        fallback_pages: set[int] = set()
        if self.use_grid_fallback:
            for page in pages:
                existing = extracted_tables.get(page, [])
                best_table = max(existing, key=lambda t: len(t.content), default=None)
                best_rows = len(best_table.content) if best_table else 0
                best_cols = (
                    max((len(c) for c in best_table.content.values()), default=0)
                    if best_table
                    else 0
                )
                page_img = page_to_image.get(page)

                needs_fallback = best_rows < self.MIN_PLAUSIBLE_ROWS
                if not needs_fallback and page_img is not None:
                    # Cheap, no-OCR shape check as a sanity reference for
                    # the column-collapse case (module docstring point 3).
                    raw_tables = TableExtractor(img=page_img).extract_tables(
                        implicit_rows=True, implicit_columns=False, borderless_tables=False
                    )
                    raw_cols = max(
                        (len(row.cells) for t in raw_tables for row in t.rows), default=0
                    )
                    if raw_cols > 0 and best_cols < raw_cols * self.MIN_COLUMN_RETENTION:
                        needs_fallback = True

                if not needs_fallback or page_img is None:
                    continue

                manual_table = _manual_grid_table(page_img, self.cell_ocr)
                if manual_table is None:
                    continue
                manual_rows = len(manual_table.content)
                manual_cols = max(
                    (len(c) for c in manual_table.content.values()), default=0
                )
                # Prefer the manual grid if it has more rows outright, or a
                # comparable row count with meaningfully more columns (the
                # column-collapse case, where row count alone looks fine).
                is_better = manual_rows > best_rows or (
                    manual_rows >= best_rows * 0.8 and manual_cols > best_cols
                )
                if is_better:
                    extracted_tables[page] = [manual_table]
                    fallback_pages.add(page)

            if fallback_pages:
                progress(
                    f"Recovered {len(fallback_pages)} page(s) via direct grid-line detection "
                    f"(img2table's own detector found nothing usable there): "
                    f"{sorted(p + 1 for p in fallback_pages)}"
                )

        return extracted_tables, fallback_pages
