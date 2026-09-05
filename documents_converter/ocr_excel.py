#!/usr/bin/env python3
"""
documents_converter.ocr_excel
------------------------------
Detects whether an input file is a scanned image or a scanned (image-based) PDF,
runs OCR + table detection on it, and exports the extracted table(s) to an Excel
(.xlsx) file.

This is the actual implementation (moved here in Phase 1 restructuring, see
docs/PHASE_0_AUDIT.md, to give the codebase a real module boundary instead of
one 952-line script). `scan_to_excel.py` at the repo root is now a thin
backward-compatible CLI wrapper around `main()` below -- run it exactly as
before:

    python scan_to_excel.py input_file [-o output.xlsx] [--lang eng] [--dpi 200]
                             [--auto-rotate] [--preprocess]
                             [--no-borderless] [--no-rotation-fix]

    Borderless-table detection and rotated-header re-OCR are ON by default
    (both were needed for full data fidelity on real documents this script
    was tested against); pass --no-borderless / --no-rotation-fix to disable.

Requirements:
    pip install img2table pytesseract openpyxl pdf2image PyMuPDF pillow

System dependency:
    Tesseract OCR engine must be installed separately.
        Ubuntu/Debian: sudo apt install tesseract-ocr
        macOS:         brew install tesseract
        Windows:       https://github.com/UB-Mannheim/tesseract/wiki
                        (then set TESSERACT_PATH below or pass --tesseract-cmd)

    For PDF page rasterization, poppler is also required by pdf2image:
        Ubuntu/Debian: sudo apt install poppler-utils
        macOS:         brew install poppler
        Windows:       https://github.com/oschwartz10612/poppler-windows
"""

import os
import re
import sys
import argparse
import shutil
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass

try:
    import numpy as np
    import cv2
    import xlsxwriter
    from img2table.document import Image, PDF
    from img2table.ocr import TesseractOCR
    from img2table.ocr._types import OCRData
    from img2table.tables.extraction import BBox, ExtractedTable, TableCell
    from img2table.tables.extractor import TableExtractor
    import pytesseract
except ImportError:
    print("Missing dependency. Install with:\n"
          "  pip install img2table pytesseract openpyxl pdf2image PyMuPDF pillow")
    sys.exit(1)


# img2table writes every extracted cell via xlsxwriter's generic write()/
# merge_range(), which (by xlsxwriter's own default) treats any string
# starting with "=" as an Excel FORMULA. OCR garbage from noisy/rotated
# scan regions can legitimately produce a leading "=" character, which then
# gets written as a broken formula -- that's exactly what Excel's "repaired
# file, removed unreadable content" dialog is stripping out on open, taking
# real data with it. Disabling strings_to_formulas at the workbook level
# (a documented xlsxwriter option) makes every string load as literal text
# instead, regardless of leading characters.
_original_workbook_init = xlsxwriter.Workbook.__init__


def _patched_workbook_init(self, filename=None, options=None):
    options = dict(options or {})
    options.setdefault("strings_to_formulas", False)
    _original_workbook_init(self, filename, options)


xlsxwriter.Workbook.__init__ = _patched_workbook_init


def _patched_group_words_by_parent(words):
    """
    Drop-in replacement for img2table's OCRData._group_words_by_parent that
    tolerates OCR words with no recognized text (value=None/empty). Upstream
    img2table (as of 2.0.0) does `" ".join(word["value"] for word in ...)`
    with no filtering, which crashes with
    "TypeError: sequence item 0: expected str instance, NoneType found"
    whenever Tesseract returns a word bounding box it couldn't read any
    characters from -- something that gets more likely with preprocessed
    (denoised/contrast-enhanced) low-quality scans. This just skips those
    words instead of crashing.
    """
    from collections import defaultdict

    parent_words = defaultdict(list)
    for word in words:
        parent_words[word.get("parent")].append(word)

    lines = [
        {
            "x1": min(word["x1"] for word in words_line),
            "y1": min(word["y1"] for word in words_line),
            "value": " ".join(
                word["value"]
                for word in sorted(words_line, key=lambda wrd: wrd["x1"])
                if word.get("value")
            ).strip(),
        }
        for words_line in parent_words.values()
    ]

    return [line["value"] for line in sorted(lines, key=lambda line: (line["y1"], line["x1"]))]


OCRData._group_words_by_parent = staticmethod(_patched_group_words_by_parent)

try:
    import fitz  # PyMuPDF - used to check text layer, and for high-DPI page rendering
except ImportError:
    fitz = None


# img2table's own PDF class rasterizes pages at a fixed ~200 DPI (scale=200/72),
# which is too low for dense/small print and is a major source of OCR errors on
# real-world scans. HighResPDF overrides page rendering to use PyMuPDF directly
# at a configurable DPI (default 300), with optional light preprocessing, while
# reusing all of img2table's table-detection/extraction/xlsx-writing logic as-is.
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


def _fix_rotated_cells(
    table,
    page_img,
    ratio_threshold: float = 1.3,
    min_height: int = 100,
    upscale: int = 3,
) -> int:
    """
    Detects and re-OCRs table cells whose text is rotated 90 degrees inside
    the cell -- a common layout for column headers in wide grade/mark sheets
    (e.g. "Life Skills Education for Policing" printed sideways so a narrow
    column can hold a long title). Tesseract reads left-to-right and cannot
    make sense of these without help, which is why they come out as noise
    like "om", "ort", "BS" in a plain OCR pass.

    Detection is purely geometric (no hardcoded column/label names, so this
    generalizes to any document with this layout): a cell whose bounding box
    is much taller than it is wide, and tall in absolute terms, is assumed to
    contain rotated text. Such cells are cropped from the full-resolution
    page image, rotated back to horizontal, upscaled (small rotated text
    tends to OCR poorly at native size), and re-OCR'd with a page-segmentation
    mode suited to a short block of text rather than img2table's own
    single-word-box hOCR pass.

    Mutates `table.content` cell values in place. Returns the number of
    cells that were successfully re-read (non-empty new value).
    :param table: ExtractedTable to fix (mutated in place)
    :param page_img: the full-resolution RGB page image the table's cell
        bounding boxes are expressed in (must be the same image img2table
        used for detection/OCR on this page)
    :param ratio_threshold: minimum height/width ratio to treat a cell as
        rotated-text (label cells that span multiple columns are wide and
        fall well under this; genuine rotated cells were observed at 1.4-11+)
    :param min_height: minimum bbox height in pixels to qualify, so a small
        single-line cell that happens to be narrow isn't misclassified
    :param upscale: linear upscale factor applied before OCR
    """
    if page_img is None:
        return 0

    gray_page = cv2.cvtColor(page_img, cv2.COLOR_RGB2GRAY)
    img_h, img_w = gray_page.shape[:2]

    ocr_cache: dict[tuple[int, int, int, int], str | None] = {}
    fixed_count = 0

    for cells in table.content.values():
        for cell in cells:
            box = cell.bbox
            w = box.x2 - box.x1
            h = box.y2 - box.y1
            if w <= 0 or h <= 0 or h < min_height or (h / w) < ratio_threshold:
                continue

            key = (box.x1, box.y1, box.x2, box.y2)
            if key not in ocr_cache:
                # Inset the crop a few pixels on every side. A cell's
                # detected bbox can be off by as little as 1px and clip in a
                # sliver of the adjacent grid line -- confirmed directly:
                # the exact same crop with y2 one pixel taller turned a
                # perfect "PPS1114" read into garbage "prsiii4", because
                # that one extra row of border pixels becomes a stray edge
                # artifact right next to the text once rotated. These cells
                # are already narrow, so trimming a small margin costs
                # little real text while reliably dropping border noise.
                margin = 3
                x1, y1 = max(box.x1 + margin, 0), max(box.y1 + margin, 0)
                x2, y2 = min(box.x2 - margin, img_w), min(box.y2 - margin, img_h)
                crop = gray_page[y1:y2, x1:x2]
                if crop.size == 0:
                    ocr_cache[key] = None
                else:
                    rotated = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
                    upscaled = cv2.resize(
                        rotated, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC
                    )
                    text = pytesseract.image_to_string(upscaled, config="--psm 6").strip()
                    ocr_cache[key] = text or None

            new_value = ocr_cache[key]
            if new_value:
                cell.value = new_value
                fixed_count += 1

    return fixed_count


def _ocr_cell_image(gray_crop, rotate: bool = False, upscale: int = 3) -> str | None:
    if gray_crop.size == 0:
        return None
    if rotate:
        gray_crop = cv2.rotate(gray_crop, cv2.ROTATE_90_CLOCKWISE)
    upscaled = cv2.resize(
        gray_crop, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC
    )
    text = pytesseract.image_to_string(upscaled, config="--psm 6").strip()
    return text or None


def _detect_grid_lines(gray, min_line_frac: float = 0.12, min_gap: int = 10):
    """
    Detects a table's row/column boundaries by direct classical line
    detection (morphological opening with long horizontal/vertical
    structuring elements) rather than relying on img2table's own table
    detector. Used as a fallback for pages where img2table finds nothing,
    or an implausibly small table, despite the page clearly containing a
    full ruled grid (confirmed on real pages in testing -- img2table found
    0 tables on several pages that this recovers cleanly).
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


def _manual_grid_table(page_img, min_rows: int = 3, min_cols: int = 3):
    """
    Reconstructs a table for one page via direct grid-line detection plus
    per-cell OCR (with the same rotated-text handling as _fix_rotated_cells),
    returning an img2table ExtractedTable so it drops into the existing
    xlsx-writing path unchanged. Returns None if no plausible grid is found.
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
            value = _ocr_cell_image(crop, rotate=rotate)

            row_cells.append(TableCell(bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2), value=value))
        content[r] = row_cells

    return ExtractedTable(
        bbox=BBox(x1=col_lines[0], y1=row_lines[0], x2=col_lines[-1], y2=row_lines[-1]),
        title=None,
        content=content,
    )


def _consensus_correct_headers(
    extracted_tables, min_group_size: int = 3, min_agreement: float = 0.34
) -> int:
    """
    Column headers (module name/code) are printed identically on every page
    of the same section, but OCR'd independently per page -- so where one
    page's read of a header cell is wrong, most other pages' reads of that
    same cell usually aren't. This pools header rows across all pages that
    share the same column count, takes the most common (plurality) value
    per column position, and corrects any page whose own value disagrees
    with that consensus. Mutates cell values in place; returns the number
    of cells corrected.

    This targets header rows specifically (not student data, which is
    genuinely different per page and has nothing to vote against) and is
    the main lever for making header accuracy trustworthy without a human
    re-typing them: agreement across independently-OCR'd pages is real
    corroborating evidence in a way a single page's OCR result never is.
    """
    # Pass 1: find header rows by label text (searching every cell in the
    # row, not just the first non-null one -- on the noisiest pages the
    # leftmost cell is often garbage rather than the actual label).
    label_matches = defaultdict(list)  # (ncols, "name"|"code") -> [(table, row_idx, cells)]
    for tables in extracted_tables.values():
        for table in tables:
            ncols = max((len(cells) for cells in table.content.values()), default=0)
            for row_idx, cells in table.content.items():
                row_text = " ".join(c.value for c in cells if c.value)
                if re.search(r"MODULE\s*NAME", row_text, re.I):
                    label_matches[(ncols, "name")].append((table, row_idx, cells))
                # "MODULE\s*C" (not the full "CODE") on purpose: a single
                # cell's OCR text can be truncated mid-word by a bad crop
                # (e.g. "MODULE C"), and there's no second cell in the row
                # to recover the missing suffix from since this label
                # doesn't repeat elsewhere in the row.
                elif re.search(r"MODULE\s*C", row_text, re.I):
                    label_matches[(ncols, "code")].append((table, row_idx, cells))

    corrected = 0
    for (ncols, _kind), matches in label_matches.items():
        if len(matches) < min_group_size:
            continue

        # Pass 2: a page whose header label is garbled beyond any
        # recognizable text won't have matched at all in pass 1 -- exactly
        # the pages most in need of correction. Recover them positionally:
        # find the row_idx most other same-shaped tables agreed was this
        # header row, and pull in that row_idx from every remaining table
        # of the same shape too.
        row_idx_counts = Counter(row_idx for _, row_idx, _ in matches)
        common_row_idx, _ = row_idx_counts.most_common(1)[0]
        matched_table_ids = {id(table) for table, _, _ in matches}
        rows = [cells for _, _, cells in matches]
        for tables in extracted_tables.values():
            for table in tables:
                if id(table) in matched_table_ids:
                    continue
                t_ncols = max((len(c) for c in table.content.values()), default=0)
                if t_ncols == ncols and common_row_idx in table.content:
                    rows.append(table.content[common_row_idx])

        if len(rows) < min_group_size:
            continue

        for col_idx in range(max(len(r) for r in rows)):
            values = [
                r[col_idx].value for r in rows if col_idx < len(r) and r[col_idx].value
            ]
            if not values:
                continue
            best_value, best_count = Counter(values).most_common(1)[0]
            if best_count / len(rows) < min_agreement:
                continue
            for r in rows:
                if col_idx < len(r) and r[col_idx].value != best_value:
                    r[col_idx].value = best_value
                    corrected += 1
    return corrected


# Heuristic "does this OCR result look wrong" checks, used to flag cells for
# human review rather than silently presenting possibly-wrong values as if
# verified -- important for a tool feeding trusted/official records, where
# no OCR pipeline can honestly promise 100% automated accuracy. These are
# deliberately generic (pattern-based, not tied to a specific column
# position) since column layout varies across sections of this document.
#
# The primary check is an ALLOWLIST, not a blocklist: garbled OCR text can
# contain almost any stray symbol (tilde, pipe, curly quotes, box-drawing
# fragments, the literal U+FFFD replacement character...), and a blocklist
# has to correctly guess every one of them in advance. Concretely: a first
# attempt at this used a blocklist that included the U+FFFD replacement
# character explicitly, expecting it to catch a garbled cell that *looked*
# like it contained "�" -- it didn't, because the actual character was
# U+2018 (a curly quote), which just renders identically to "�" in some
# terminal fonts. An allowlist of what legitimate content in *this*
# document actually looks like (Latin letters, digits, basic punctuation)
# catches that and any other unanticipated noise character in one rule,
# rather than needing the blocklist extended every time OCR produces a new
# kind of garbage.
_ALLOWED_CHARS_RE = re.compile(r"^[A-Za-z0-9À-ſ\s,./%()'&:;!-]*$")

_SUSPICIOUS_PATTERNS = [
    re.compile(r"\d\.\d\d\b"),          # period-decimal in a comma-decimal document
    re.compile(r"\bNone\b"),            # a literal "None" leaking into OCR'd text
    re.compile(r"\d.*\n.*\d.*\n.*\d"),  # 3+ numeric fragments crammed into one cell
]


def _is_suspicious(value) -> bool:
    if not value:
        return False
    if not _ALLOWED_CHARS_RE.match(value):
        return True
    return any(pat.search(value) for pat in _SUSPICIOUS_PATTERNS)


def _write_table_flagged(table, sheet, normal_fmt, flag_fmt) -> int:
    """
    Writes a table's cells directly (one cell per column position, rather
    than img2table's merge_range-based writer) so each cell can get its own
    format -- flagged cells get a highlighted background so a human
    reviewer can find exactly what to double-check, instead of the file
    silently presenting uncertain OCR output as if it were verified.
    Returns the number of cells flagged.
    """
    flagged = 0
    for row_idx, cells in table.content.items():
        for col_idx, cell in enumerate(cells):
            suspicious = _is_suspicious(cell.value)
            sheet.write(row_idx, col_idx, cell.value, flag_fmt if suspicious else normal_fmt)
            if suspicious:
                flagged += 1
    sheet.autofit()
    return flagged


def _pick_extraction_mode(page_img, min_rows: int = 5) -> bool:
    """
    Cheaply (no OCR -- pure shape detection) compares bordered vs borderless
    table detection for one page and returns whether borderless_tables
    should be used. Confirmed on real documents that neither mode is
    uniformly better, so a single fixed choice for a whole document is
    unsafe: one document needed borderless to find a page's table at all
    (the bordered/strict-grid detector found nothing there), while a
    DIFFERENT document got WORSE under borderless -- it fragmented one
    clean, complete table into multiple broken pieces that bordered alone
    had found correctly as a single table. Deciding per page from actual
    evidence on that page avoids both failure modes.
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


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}


def is_scanned_pdf(file_path: str, text_char_threshold: int = 20) -> bool:
    """
    Heuristic check: if a PDF has (almost) no extractable text layer, it's
    treated as a scanned/image-based PDF. If PyMuPDF isn't installed, we
    fall back to assuming it's scanned (OCR will just run and it's harmless
    if there was a text layer too).
    """
    if fitz is None:
        return True

    try:
        doc = fitz.open(file_path)
        total_chars = 0
        pages_checked = min(len(doc), 3)  # sample first few pages for speed
        for i in range(pages_checked):
            total_chars += len(doc[i].get_text().strip())
        doc.close()
        return total_chars < text_char_threshold
    except Exception as e:
        print(f"Warning: could not inspect PDF text layer ({e}). Assuming scanned.")
        return True


def detect_file_type(file_path: str) -> str:
    """Returns 'image', 'scanned_pdf', 'text_pdf', or raises ValueError."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"No such file: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext == ".pdf":
        return "scanned_pdf" if is_scanned_pdf(file_path) else "text_pdf"
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


def check_tesseract_available(tesseract_cmd: str = None) -> bool:
    if tesseract_cmd:
        return os.path.isfile(tesseract_cmd)
    return shutil.which("tesseract") is not None


def convert_scanned_to_excel(
    file_path: str,
    output_excel_path: str,
    lang: str = "eng",
    borderless_tables: bool = True,
    tesseract_cmd: str = None,
    n_threads: int = 2,
    dpi: int = 200,
    auto_rotate: bool = False,
    preprocess: bool = False,
    fix_rotated_headers: bool = True,
    use_grid_fallback: bool = True,
    consensus_fix_headers: bool = True,
    flag_suspicious_cells: bool = True,
    auto_detect_mode: bool = True,
) -> str:
    """
    Detects file type (image / scanned PDF / text PDF) and exports detected
    tables to an .xlsx file. Returns the output path on success.
    """
    if not check_tesseract_available(tesseract_cmd):
        raise EnvironmentError(
            "Tesseract OCR engine not found on PATH. Install it first "
            "(see script docstring), or pass --tesseract-cmd with the full path "
            "to the tesseract executable."
        )

    if tesseract_cmd:
        # img2table's TesseractOCR shells out to the literal command
        # "tesseract" (via subprocess, not through pytesseract), so the
        # only way to point it at a non-PATH install is to make sure the
        # folder containing the binary is on PATH for this process (and
        # anything it spawns) before TesseractOCR is constructed.
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        tess_dir = os.path.dirname(tesseract_cmd)
        if tess_dir and tess_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = tess_dir + os.pathsep + os.environ.get("PATH", "")

    file_type = detect_file_type(file_path)
    print(f"Detected file type: {file_type}")

    ocr = TesseractOCR(n_threads=n_threads, lang=lang)

    if file_type == "image":
        doc = Image(src=file_path, detect_rotation=auto_rotate)
    else:
        # Both scanned and text-layer PDFs work fine through img2table;
        # OCR is simply redundant (but harmless) for a text-layer PDF.
        # HighResPDF gives explicit control over rendering DPI (img2table's
        # built-in PDF class hardcodes ~200 DPI with no way to change it).
        # Default matches that built-in value since it's what img2table's
        # own table-detection is tuned against; only raise it if testing on
        # your specific document confirms it helps rather than hurts.
        doc = HighResPDF(
            src=file_path,
            dpi=dpi,
            preprocess=preprocess,
            detect_rotation=auto_rotate,
        )

    # PDF/HighResPDF only populates .pages lazily (as a side effect of
    # .images or .extract_tables()); force it now since the auto-mode path
    # below needs the full page list before calling extract_tables(). Image
    # sets .pages directly at construction and has no such method.
    if hasattr(doc, "_ensure_pages"):
        doc._ensure_pages()
    pages = getattr(doc, "pages", None) or [0]
    page_to_image = {page_num: doc.images[k] for k, page_num in enumerate(pages)}

    print("Extracting tables...")
    if auto_detect_mode and file_type != "image":
        # Decide bordered-vs-borderless per page from actual evidence on
        # that page, rather than one fixed choice for the whole document --
        # confirmed necessary on real documents: one needed borderless to
        # find a page's table at all, while a DIFFERENT document got worse
        # under borderless (fragmented one clean table into broken pieces
        # bordered alone had found correctly). Grouped into two batched OCR
        # passes rather than one call per page, so this doesn't double the
        # OCR cost of the whole document.
        borderless_pages = [p for p in pages if _pick_extraction_mode(page_to_image[p])]
        bordered_pages = [p for p in pages if p not in borderless_pages]
        if borderless_pages and bordered_pages:
            print(
                f"Auto-selected table-detection mode per page: "
                f"{len(bordered_pages)} page(s) bordered, "
                f"{len(borderless_pages)} page(s) borderless."
            )

        extracted_tables = {}
        for mode, mode_pages in ((False, bordered_pages), (True, borderless_pages)):
            if not mode_pages:
                continue
            sub_doc = HighResPDF(
                src=file_path,
                dpi=dpi,
                preprocess=preprocess,
                detect_rotation=auto_rotate,
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

    if use_grid_fallback:
        # img2table's own detector can fail outright on some pages (0
        # tables), return an implausibly small result, or -- confirmed on a
        # real document -- quietly collapse columns during its own
        # OCR-based refinement step (one page's raw geometric detection
        # found 20 columns; after OCR that same table shrank to 16, with
        # the dropped trailing columns' values replaced by leftover header
        # text repeated into every row). Any of these silently loses real
        # data, so fall back to direct grid-line detection (classical CV,
        # bypassing img2table's detector and its content-refinement step
        # entirely) rather than accepting a quietly-truncated result.
        MIN_PLAUSIBLE_ROWS = 8
        MIN_COLUMN_RETENTION = 0.85
        fallback_pages = set()
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

            needs_fallback = best_rows < MIN_PLAUSIBLE_ROWS
            if not needs_fallback and page_img is not None:
                # Cheap, no-OCR shape check as a sanity reference for the
                # column-collapse case above.
                raw_tables = TableExtractor(img=page_img).extract_tables(
                    implicit_rows=True, implicit_columns=False, borderless_tables=False
                )
                raw_cols = max((len(row.cells) for t in raw_tables for row in t.rows), default=0)
                if raw_cols > 0 and best_cols < raw_cols * MIN_COLUMN_RETENTION:
                    needs_fallback = True

            if not needs_fallback or page_img is None:
                continue

            manual_table = _manual_grid_table(page_img)
            if manual_table is None:
                continue
            manual_rows = len(manual_table.content)
            manual_cols = max((len(c) for c in manual_table.content.values()), default=0)
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
            print(
                f"Recovered {len(fallback_pages)} page(s) via direct grid-line detection "
                f"(img2table's own detector found nothing usable there): "
                f"{sorted(p + 1 for p in fallback_pages)}"
            )

    if fix_rotated_headers:
        # Cells from the grid-line fallback already went through the same
        # rotation-aware OCR during their own construction; no need to
        # redo it for those pages.
        total_fixed = 0
        for page, tables in extracted_tables.items():
            if page in fallback_pages if use_grid_fallback else False:
                continue
            page_img = page_to_image.get(page)
            for table in tables:
                total_fixed += _fix_rotated_cells(table, page_img)
        if total_fixed:
            print(f"Re-OCR'd {total_fixed} rotated-text cell(s) (e.g. sideways column headers).")

    if consensus_fix_headers:
        # Module name/code headers repeat identically across every page of
        # a section; independent pages agreeing with each other is real
        # corroborating evidence, so use it to correct outlier header reads
        # rather than trusting each page's OCR result in isolation.
        n_corrected = _consensus_correct_headers(extracted_tables)
        if n_corrected:
            print(
                f"Corrected {n_corrected} header cell(s) using cross-page agreement "
                f"(same module name/code printed on multiple pages)."
            )

    print("Writing to Excel...")
    workbook = xlsxwriter.Workbook(output_excel_path, {"in_memory": True})
    cell_format = workbook.add_format({"align": "center", "valign": "vcenter", "text_wrap": True})
    cell_format.set_border()
    flag_format = workbook.add_format({
        "align": "center", "valign": "vcenter", "text_wrap": True, "bg_color": "#FFF2AC",
    })
    flag_format.set_border()

    total_flagged = 0
    for page, tables in extracted_tables.items():
        for idx, table in enumerate(tables):
            sheet = workbook.add_worksheet(name=f"Page {page + 1} - Table {idx + 1}")
            if flag_suspicious_cells:
                total_flagged += _write_table_flagged(table, sheet, cell_format, flag_format)
            else:
                table._to_worksheet(sheet=sheet, cell_fmt=cell_format)
    workbook.close()

    if flag_suspicious_cells and total_flagged:
        print(
            f"\nFlagged {total_flagged} cell(s) (highlighted yellow) whose OCR text looks "
            f"suspicious -- e.g. stray symbols, mismatched decimal separators, or leftover "
            f"'None' from a merged/failed read. This is NOT a claim those cells are wrong, "
            f"only that they didn't pass a basic sanity check and are worth a manual glance "
            f"before treating this as an official record. No OCR pipeline can honestly "
            f"guarantee 100% automated accuracy on scanned documents -- this highlighting is "
            f"how to keep that honest rather than hide it."
        )

    print(f"Done. Excel file saved to: {output_excel_path}")
    return output_excel_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert a scanned image or scanned PDF into an Excel file."
    )
    parser.add_argument("input_file", help="Path to the scanned image or PDF")
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output .xlsx path (default: same name as input, .xlsx extension)"
    )
    parser.add_argument("--lang", default="eng", help="Tesseract language code (default: eng)")
    parser.add_argument(
        "--no-borderless", dest="borderless", action="store_false",
        help="Only takes effect together with --no-auto-mode: fixes bordered-table "
             "detection for every page instead of letting --auto-mode decide per page. "
             "By default (--auto-mode on) this flag is ignored, because a single fixed "
             "choice isn't safe across documents -- confirmed on real documents that "
             "borderless helps some pages (recovers a table the strict grid detector "
             "misses entirely) and actively hurts others (fragments one clean table "
             "into broken pieces bordered alone found correctly)."
    )
    parser.add_argument(
        "--no-auto-mode", dest="auto_detect_mode", action="store_false",
        help="Disable per-page bordered-vs-borderless auto-selection (default: "
             "enabled) and use a single fixed choice (--borderless/--no-borderless) "
             "for every page instead. Auto-mode compares both detectors on each page "
             "(cheaply, before OCR) and uses whichever gives a more plausible result "
             "there -- turn it off only if you've confirmed the fixed choice works "
             "better on your specific document."
    )
    parser.add_argument(
        "--tesseract-cmd", default=None,
        help="Full path to tesseract executable (mainly needed on Windows)"
    )
    parser.add_argument(
        "--dpi", type=int, default=200,
        help="Rasterization resolution for scanned PDFs (default: 200, matching "
             "img2table's own built-in rendering). Table-border detection is tuned "
             "around this value -- raising it can IMPROVE text sharpness but also "
             "HURT table detection on some documents (grid lines become 'too thick' "
             "in pixel terms). Test on your document before relying on a higher value."
    )
    parser.add_argument(
        "--auto-rotate", action="store_true",
        help="Auto-detect and correct page rotation/skew before OCR"
    )
    parser.add_argument(
        "--preprocess", action="store_true",
        help="Apply denoising + contrast enhancement before OCR "
             "(helps low-contrast/noisy scans; leave off if it hurts border detection)"
    )
    parser.add_argument(
        "--no-rotation-fix", dest="fix_rotated_headers", action="store_false",
        help="Skip re-OCR'ing cells whose text is rotated 90 degrees (default: enabled). "
             "These are detected geometrically (tall/narrow cell), cropped, rotated back "
             "to horizontal, upscaled and re-read -- fixes sideways column headers common "
             "in wide grade/mark sheets, which plain OCR reads as noise."
    )
    parser.add_argument(
        "--no-grid-fallback", dest="use_grid_fallback", action="store_false",
        help="Disable the direct grid-line-detection fallback (default: enabled). "
             "img2table's own table detector can silently drop entire pages (return 0 "
             "tables, or an implausibly small one) even when the page has a full, "
             "clearly-ruled table -- confirmed on real documents. When that happens this "
             "reconstructs the table directly from detected grid lines instead of losing "
             "the page's data."
    )
    parser.add_argument(
        "--no-consensus-fix", dest="consensus_fix_headers", action="store_false",
        help="Disable cross-page consensus correction for header cells (default: enabled). "
             "Module name/code headers repeat identically across every page of a section; "
             "this takes the most common reading across pages and corrects outlier pages "
             "to match, since independent agreement is real evidence a single page's OCR "
             "isn't."
    )
    parser.add_argument(
        "--no-flag-suspicious", dest="flag_suspicious_cells", action="store_false",
        help="Disable highlighting of cells whose OCR text fails a basic sanity check "
             "(default: enabled). Recommended to leave ON for documents feeding official "
             "records: flagged (yellow) cells are not necessarily wrong, but are worth a "
             "manual glance -- no OCR pipeline can honestly guarantee 100%% automated "
             "accuracy, so surfacing uncertainty beats hiding it."
    )
    args = parser.parse_args()

    output_path = args.output or (
        os.path.splitext(args.input_file)[0] + ".xlsx"
    )

    try:
        convert_scanned_to_excel(
            file_path=args.input_file,
            output_excel_path=output_path,
            lang=args.lang,
            borderless_tables=args.borderless,
            tesseract_cmd=args.tesseract_cmd,
            dpi=args.dpi,
            auto_rotate=args.auto_rotate,
            preprocess=args.preprocess,
            fix_rotated_headers=args.fix_rotated_headers,
            use_grid_fallback=args.use_grid_fallback,
            consensus_fix_headers=args.consensus_fix_headers,
            flag_suspicious_cells=args.flag_suspicious_cells,
            auto_detect_mode=args.auto_detect_mode,
        )
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


# Deliberately no `if __name__ == "__main__": main()` here -- that's the
# repo-root scan_to_excel.py wrapper's job, so this module can be imported
# (by that wrapper, by tests, or by a future caller) without side effects.
