# Scanned PDF/Image → Excel Converter

Detects whether an input file is a scanned image or a scanned (image-based) PDF,
runs OCR + table detection on it, and exports the extracted table(s) to an
Excel (`.xlsx`) file — built and hardened specifically for dense, multi-page
grade/mark-sheet style tables.

## Project layout

```
scan_to_excel.py                     thin CLI wrapper (unchanged usage)
documents_converter/
    __init__.py
    ocr_excel.py                     pipeline orchestration (file-type detection,
                                      calling the providers below in order, writing
                                      the .xlsx, the Excel/img2table bugfix patches)
    providers/
        cell_ocr.py                  CellOCRProvider: recognizes text in one
                                      already-cropped cell image (used by the
                                      rotated-header fix and the grid fallback)
        table_detection.py           TableDetector: per-page bordered/borderless
                                      mode selection, img2table extraction, and
                                      the grid-line-detection fallback
    api/
        app.py                       minimal synchronous HTTP API (see below)
        config.py                    environment-based API configuration
        security.py                  magic-byte + decompression-bomb checks
        rate_limit.py                per-IP fixed-window rate limiter
tests/
    conftest.py
    test_ocr_excel.py                 pipeline/provider regression tests
    test_api.py                        API tests
    fixtures/synthetic_scan.py        generates a fabricated (no real data) test PDF
docs/
    PHASE_0_AUDIT.md                  current-state audit, capability matrix, phase plan
.github/workflows/test.yml            CI: runs the test suite on every push/PR
Dockerfile                            containerizes the API (not the CLI)
```

`documents_converter/ocr_excel.py` orchestrates the pipeline and calls into
`providers/` for the two things most likely to need a different engine some
day (per-cell OCR, table detection); `scan_to_excel.py` is kept at the repo
root as a thin wrapper so existing usage (`python scan_to_excel.py ...`)
keeps working unchanged.

## Setup (Windows)

1. Install Python packages:
   ```powershell
   pip install -r requirements.txt
   # add -r requirements-dev.txt too if you want to run the test suite
   ```

2. Install the Tesseract OCR engine (not a Python package — a separate binary):
   - Download the installer: https://github.com/UB-Mannheim/tesseract-ocr/wiki
     (UB-Mannheim builds; get the 64-bit `.exe`)
   - Install it, then either:
     - add its install folder (default `C:\Program Files\Tesseract-OCR`) to your
       system `PATH`, **or**
     - pass its full path at run time with `--tesseract-cmd`.

3. Install Poppler (needed for PDF page rasterization by `pdf2image`):
   - Download: https://github.com/oschwartz10612/poppler-windows/releases
   - Unzip it somewhere (e.g. `C:\poppler`) and add its `Library\bin` folder
     to your system `PATH`.

4. Restart your terminal so the updated `PATH` takes effect, then verify:
   ```powershell
   tesseract --version
   pdftoppm -v
   ```

## Usage

```powershell
python scan_to_excel.py input_file.pdf -o output.xlsx
python scan_to_excel.py scanned_image.png
```

Run `python scan_to_excel.py -h` for the full flag list. Everything listed
there defaults to **on** except `--dpi`, `--auto-rotate`, and `--preprocess`
— the defaults are the tested-safe configuration; only override them if
you've confirmed on your own document that the override actually helps
(see the DPI/preprocess note below, both of which measured *worse* than
the default on the real document this was tuned against).

## HTTP API (optional)

A minimal synchronous API wraps the same engine, for anything that needs
to call this over HTTP instead of the CLI. It is deliberately **not** a
job queue: the request stays open until the conversion finishes, since
that's the right amount of infrastructure for a first API (see
`docs/PHASE_0_AUDIT.md` Phase 3) — a queue only earns its complexity once
real usage shows requests taking long enough to need one.

```powershell
pip install -r requirements.txt -r requirements-api.txt

# TESSERACT_CMD only needed if tesseract isn't already on PATH
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
uvicorn documents_converter.api.app:app --reload
```

```
GET  /health            -> {"status": "ok", "tesseract_available": true}
POST /api/v1/convert    -> upload a file (multipart/form-data, field name
                            "file"), get the .xlsx back in the response body
```

Example:
```powershell
curl.exe -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o result.xlsx
```

Configuration (environment variables, see `documents_converter/api/config.py`):
`TESSERACT_CMD` (default: none, i.e. must be on `PATH`), `MAX_UPLOAD_MB`
(default: 50), `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`
(default: 10 requests per 60s per client IP), `CONVERT_TIMEOUT_SECONDS`
(default: 180).

### Security hardening

Beyond Phase 3's basic hygiene (extension allowlist, safe temp-file
naming, no document-content logging), the API also has:

- **Magic-byte validation** — the file's actual content must match a
  known signature for the extension it claims (`documents_converter/api/security.py`).
  A `.pdf` that isn't really a PDF is rejected before any processing.
- **Decompression-bomb limits** — a PDF's page count and an image's
  decompressed pixel dimensions are checked (200 pages / 50 megapixels by
  default) *before* the expensive OCR pipeline runs, since a small file
  can still decompress into something that exhausts memory.
- **Per-IP rate limiting** — a simple in-memory fixed-window limiter
  (`documents_converter/api/rate_limit.py`). Deliberately not backed by
  Redis: correct for the single-process deployment this project currently
  is, but it won't share state across multiple replicas — a real
  multi-instance deployment needs a shared store instead.
- **Best-effort conversion timeout** — a hung or pathological file won't
  hold a request open forever. "Best-effort" because Python has no safe
  API to force-kill a thread; an abandoned conversion keeps running in
  the background until it finishes, it's just no longer waited on.
- **No leaked stack traces** — a catch-all exception handler guarantees
  any unexpected error returns a generic message, regardless of what
  actually went wrong.

Still explicitly out of scope (see `docs/PHASE_0_AUDIT.md` Phase Plan):
authentication, and the rate limiter's single-process limitation above.
Not yet suitable for untrusted public traffic without those.

### Running the API in Docker

```powershell
docker build -t documents-converter-api .
docker run -p 8000:8000 documents-converter-api
```

Tesseract is installed inside the image (`apt-get install tesseract-ocr`),
so no `TESSERACT_CMD` is needed there. Verified locally: built the image,
ran it, and confirmed a real conversion through the container produces the
same correct data as running natively — worth knowing if you compare
outputs closely, the *blank*-cell noise can differ slightly page to page,
since the Linux `apt` Tesseract build reads faint empty-cell artifacts a
little differently than the Windows build used elsewhere in this project;
that's cosmetic, not a correctness issue (see the Accuracy & trust report
below on this class of noise generally).

## Continuous integration

`.github/workflows/test.yml` runs the full test suite on every push and
pull request to `main` — added specifically because nothing was catching
a regression automatically before this, despite the real test coverage
Phases 1-4 built to guard against real bugs found during this project's
development.

## Running tests

```powershell
pip install -r requirements.txt -r requirements-api.txt -r requirements-dev.txt
pytest tests/ -v
```

(`requirements-api.txt` is needed too since `tests/test_api.py` imports the
API app.)

Tests run against a synthetic, fabricated fixture generated on the fly
(`tests/fixtures/synthetic_scan.py`) — never real scanned documents, which
contain genuine personal data (see the Accuracy & trust report below and
`docs/PHASE_0_AUDIT.md` risk register). Each test's docstring names the
specific real bug it guards against — these aren't speculative edge cases,
they're regressions this project actually hit during development. One
end-to-end test needs a real Tesseract install and skips automatically if
it can't find one on `PATH` or at the default Windows install location.

---

## Accuracy & trust report

This section exists because this tool has been proposed for feeding
**official records**. Read this before trusting its output for that.

### The honest baseline

**No OCR pipeline — this one included — can honestly promise 100%
automated accuracy on a real scanned document.** That is a fundamental
limit of OCR technology, not a gap specific to this script. Anyone telling
you otherwise about any OCR tool is not being straight with you. What a
well-engineered pipeline *can* do is (1) maximize accuracy through
cross-checking and structural validation, and (2) make its own remaining
uncertainty visible rather than hiding it. Both are built in here — see
below — but neither is a substitute for a human reviewing the flagged
output before it becomes an official record.

### What's been verified, concretely, not assumed

Every claim below was checked by rendering the actual source PDF page as
an image and comparing it cell-by-cell against the extracted spreadsheet
— not inferred from confidence scores or spot-guessed:

- **Module/course codes**: 100% correct across every sample checked
  (dozens of codes across two different documents).
- **Numeric grades**: verified exact matches in the large majority of
  cells checked — e.g. one student's full 9-module grade row, total
  credit count, and average all matched the source exactly; another had
  8 of 9 grades exact with one single-digit slip. That ratio (occasional
  single-character misreads, never wholesale wrong values) is
  representative of what testing found across both documents.
- **Names and IDs**: correct in the large majority of cases; occasional
  garbling when a name sits in a merged/under-segmented cell region.
- **Page coverage**: 100% of pages produced output on both test
  documents, after fixing table-detection failures that were originally
  silently dropping whole pages.
- **File integrity**: zero instances of the Excel-formula-injection
  corruption bug (see Engineering below) across every regenerated file.

### What's NOT reliable without review

- **Isolated single-digit/single-character misreads on otherwise
  well-formatted values.** This is the hardest class of error to catch
  automatically, by nature: a misread digit that still looks like a
  plausible number (`78,52` instead of `78,50`) passes every automated
  sanity check there is. No heuristic — including the ones in this
  script — can catch a wrong-but-plausible value without a second,
  independent source of truth. This is the main reason a human should
  proofread the final numbers before they're treated as official.
- **Merged-cell "None" artifacts**: on some rows, two adjacent OCR reads
  bleed into one cell (e.g. `"None 80,50 None 78,50"`). Confirmed: every
  instance of this found during testing was correctly caught by the
  suspicious-cell flagging below — but this class of error is not yet
  eliminated at the source, only reliably surfaced for review.
- **Header cells on pages that are structurally unlike every other page**
  in the document (too few "peer" pages sharing the same column layout
  to vote with) don't benefit from cross-page correction and are more
  likely to still show OCR noise.

### The two safeguards built specifically for "trusted document" use

1. **Cross-page consensus correction** — module name/code headers repeat
   identically across every page of a document section. Independent
   pages agreeing with each other is real corroborating evidence a single
   page's OCR result never has on its own; this pools all pages sharing
   the same layout, takes the majority reading per column, and corrects
   any outlier page to match. Verified: on a 35-page test document this
   corrected 526 header cells; spot-checked before/after and confirmed
   cells that were pure noise became exactly correct course codes.

2. **Suspicious-cell flagging (yellow highlight)** — every cell is
   checked against an *allowlist* of characters that can legitimately
   appear in this kind of document (letters, digits, standard
   punctuation). Anything else — a stray symbol, a mismatched decimal
   separator, a leftover literal "None" from a failed merge — gets
   highlighted yellow in the output. This is deliberately not a claim
   that a flagged cell *is* wrong, only that it didn't pass a basic
   sanity check and is worth a manual glance. Verified: in every
   confirmed real error found during testing (a `76.90` where `76,00`
   was correct; a `$6,00` where `66,00` was correct; every "None"-merge
   artifact checked), the flagging system caught it. **Recommended
   workflow for official use: review every yellow cell before treating
   the output as a record. Don't disable this flag for that use case.**

### Practical recommendation

Use this as an **OCR-assisted data-entry accelerator with mandatory human
review of every flagged cell**, not as a fully-automated, zero-touch
pipeline for legally-binding records. That's not a weakness specific to
this script — it's the honest operating envelope of OCR technology on
real scanned paper. Within that envelope, this pipeline has been
engineered and tested to get as close to full automation as the
technology allows, and to be transparent about exactly where it isn't
certain.

---

## Notes

- Works on `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`.
- For PDFs, the script auto-detects whether there's an extractable text layer;
  image-based (scanned) PDFs get routed through OCR, text-based PDFs are
  parsed directly (OCR still runs but has little effect).
- Bordered-vs-borderless table detection is chosen **per page, automatically**
  (`--no-auto-mode` to disable): tested on real documents where a single
  fixed choice for the whole file was actively wrong for some pages either
  way — one page needed borderless to be detected at all, while forcing
  borderless on another page fragmented an otherwise-clean table.
- If `img2table`'s own detector fails a page outright, or silently drops
  columns during its internal OCR-refinement step (confirmed on a real
  document: 20 genuine columns collapsed to 16, with a header label copied
  into every data row of the dropped columns), a direct grid-line detector
  (classical CV, bypassing that step entirely) reconstructs the table
  instead of losing the data. `--no-grid-fallback` to disable.

### On `--dpi` and `--preprocess` (tested, not assumed)

These were added expecting them to improve accuracy, then actually tested
against a real 35-page scanned document and measured — the results were
counterintuitive, so don't reach for them by default:

- **`--dpi` above the default (200) made things worse** on the test document:
  raising it to 300 caused `img2table`'s table-border detection (tuned around
  ~200 DPI pixel thickness) to miss tables on more than half the pages that
  200 DPI found correctly. Only raise it if you've confirmed on your own file
  that detection still finds at least as many tables at the higher setting.
- **`--preprocess` (denoise + contrast enhancement) also made things worse**
  on the test document: rows that OCR'd cleanly at plain 200 DPI came out
  more corrupted with it on (more merged/missing cells). It's left in as an
  opt-in experiment for documents that are genuinely low-contrast/noisy, but
  verify it actually helps on a sample page before trusting it for a full run.
- **The setting that actually worked best was the plain default** (no `--dpi`
  override, no `--preprocess`).

### Known accuracy limitation: rotated/vertical header text

If your source document has column headers printed sideways (rotated 90°)
inside table cells — common in dense grade/mark sheets — those cells used to
come out completely garbled (Tesseract reads left-to-right and doesn't
auto-detect per-cell rotation). This is now handled: such cells are detected
geometrically (tall/narrow bounding box), cropped, rotated back to
horizontal, upscaled, and re-OCR'd (`--no-rotation-fix` to disable). Verified
on a real document going from unreadable noise to correct course titles and
codes.

For documents that are broadly low-quality throughout — not just rotated
headers — an LLM-vision approach (sending the page image to a vision-capable
model and asking for structured JSON, then loading that into pandas) may do
better than Tesseract in some cases, at the cost of per-document API calls
and its own, different failure modes. Not built into this script.
