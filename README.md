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
    registry.py                      Format & Capability Registry -- the single
                                      source of truth for which (source format ->
                                      target format) conversions this service can
                                      do, and what performs each one
    converters/                      one module per registered Capability
        ocr_to_excel.py               wraps the OCR pipeline above as a Capability
        image_to_pdf.py               image -> PDF (no OCR) -- the proof that the
                                      registry isn't OCR-only
    api/
        app.py                       minimal synchronous HTTP API (see below)
        config.py                    environment-based API configuration
        security.py                  magic-byte + decompression-bomb checks
        rate_limit.py                per-IP fixed-window rate limiter
        auth.py                      API-key authentication
        jobs.py                      in-memory async job store
        static/index.html            the web page -- upload, convert, download
tests/
    conftest.py
    test_ocr_excel.py                 pipeline/provider regression tests
    test_registry.py                   capability registry unit tests
    test_api.py                        API tests
    test_frontend.py                   real-browser (Playwright) frontend tests
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

## Web page

Open `http://127.0.0.1:8000/` (or wherever the API is running) in a
browser for a real, no-curl-required page: choose or drag a file, click
Convert, watch it process, and the finished `.xlsx` downloads
automatically. A single self-contained HTML file
(`documents_converter/api/static/index.html`, inline CSS/JS, no build
step) served directly by the API, calling the same `/api/v1/jobs`
endpoints documented below — nothing here has its own state or logic
beyond what those endpoints already provide and already have tests for.

Verified with a real headless browser (Playwright), not just by reading
the HTML: `tests/test_frontend.py` actually loads the page, picks a file
through the real file input, clicks the real button, and confirms a real
file downloads with correct data — the same standard as every other
end-to-end test in this project.

Known limitation: the page always submits to the default `target`
(OCR→Excel) — it doesn't yet expose the other conversions the registry
below knows about (e.g. image→PDF). Reaching those currently means
calling the API directly with an explicit `target` field.

## Format & capability registry

`documents_converter/registry.py` is the single source of truth for which
(source format → target format) conversions this service can perform, and
what actually performs each one — added so a new conversion plugs in
without hardcoding another special case into the API layer (`if ext ==
".pdf" and target == "xlsx": ... elif ...`, which only gets worse with
every conversion after the first). Both `/api/v1/convert` and
`/api/v1/jobs` route through it via an optional `target` field.

Two capabilities are registered today:

| source format      | target | accepts                                    | what it does                          |
|---------------------|--------|---------------------------------------------|----------------------------------------|
| `scanned_document`   | `xlsx` | `.pdf .png .jpg .jpeg .tiff .tif .bmp`      | the OCR + table-detection pipeline above |
| `image`              | `pdf`  | `.png .jpg .jpeg .tiff .tif .bmp`           | plain image → single-page PDF, no OCR  |

`GET /api/v1/capabilities` reports this list live, from the registry
itself, so it can't drift out of sync with what the server actually does:

```powershell
curl.exe http://127.0.0.1:8000/api/v1/capabilities
```

Adding a new conversion means writing one new module under
`documents_converter/converters/` exposing a `Capability` and registering
it in that package's `__init__.py` — nothing in `app.py`'s routing logic
needs to change, since it only ever asks the registry "what handles this
(extension, target) pair?" (`registry.find`, used by `app._resolve_capability`).

## HTTP API (optional)

An API wraps the registry above, for anything that needs to call this over
HTTP instead of the CLI (the web page above is itself just a client of
it). Two ways to call it:

```powershell
pip install -r requirements.txt -r requirements-api.txt

# TESSERACT_CMD only needed if tesseract isn't already on PATH
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
uvicorn documents_converter.api.app:app --reload
```

```
GET  /health                    -> {"status": "ok", "tesseract_available": true}
GET  /api/v1/capabilities       -> what conversions are registered (see above)

POST /api/v1/convert            -> synchronous: upload a file (multipart/form-data,
                                    field name "file") and optionally "target" (a
                                    target format from /api/v1/capabilities; defaults
                                    to "xlsx"). The response IS the finished file.
                                    Simplest option; the connection stays open for
                                    the whole conversion.

POST /api/v1/jobs                -> async: same fields ("file", optional "target"),
                                    get back {"job_id": "...", "status": "queued"}
                                    immediately (202). The conversion runs in the
                                    background.
GET  /api/v1/jobs/{id}           -> {"job_id": "...", "status": "queued|processing|
                                    completed|failed", "error": "..." (if failed)}
GET  /api/v1/jobs/{id}/result    -> the finished file, once status is "completed"
                                    (409 otherwise) -- Content-Type and filename
                                    extension match whichever capability ran.
```

Synchronous example (defaults to the OCR->Excel capability):
```powershell
curl.exe -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o result.xlsx
```

Synchronous example, a different target (the image->pdf capability):
```powershell
curl.exe -F "target=pdf" -F "file=@photo.png" http://127.0.0.1:8000/api/v1/convert -o result.pdf
```

Async example:
```powershell
curl.exe -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/jobs
# {"job_id": "a1b2c3...", "status": "queued"}
curl.exe http://127.0.0.1:8000/api/v1/jobs/a1b2c3...
# poll until "status": "completed"
curl.exe http://127.0.0.1:8000/api/v1/jobs/a1b2c3.../result -o result.xlsx
```

Use the synchronous endpoint for quick/small conversions where holding a
connection open briefly is fine; use the job endpoints for anything slow
enough that you'd rather not block a client on it, or where the caller
isn't well-suited to holding a connection open at all. Both share the
same validation and the same worker pool — see `documents_converter/api/app.py`'s
module docstring for the resulting known limitation (heavy async load
can delay sync requests).

The job store (`documents_converter/api/jobs.py`) is in-memory, the same
honest limitation as the rate limiter below: correct for a single-process
deployment, but jobs don't survive a restart and aren't visible across
multiple replicas. Finished jobs' files are cleaned up after
`JOB_RETENTION_SECONDS` (default: 1 hour).

Configuration (environment variables, see `documents_converter/api/config.py`):
`TESSERACT_CMD` (default: none, i.e. must be on `PATH`), `MAX_UPLOAD_MB`
(default: 50), `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`
(default: 10 requests per 60s per client IP), `CONVERT_TIMEOUT_SECONDS`
(default: 180), `JOB_RETENTION_SECONDS` (default: 3600), `API_KEYS`
(default: empty, i.e. auth off — see below).

### Authentication

Every `/api/v1/*` route (`/convert` and all three `/jobs` routes) requires
an API key once `API_KEYS` is set to a comma-separated list; `GET /health`
never does (load balancers and monitoring probes need to reach it without
credentials). Empty by default so local/dev use needs no extra setup —
set it before exposing this to anything other than trusted local use.

```powershell
$env:API_KEYS = "some-long-random-key,another-key-for-a-second-caller"
```

```powershell
curl.exe -H "Authorization: Bearer some-long-random-key" -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o result.xlsx
```

This is deliberately API keys, not user accounts — there's no database
yet, and building one before there's an actual need for per-user data
(history, usage billing) would repeat exactly the kind of premature
infrastructure `docs/PHASE_0_AUDIT.md` warns against. A real identity
system is a reasonable later phase once that need exists.

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

Combined with the Authentication section above, the API is now closer to
suitable for untrusted traffic — the main remaining gap is the rate
limiter's single-process limitation noted there, which matters once this
runs as more than one replica.

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
playwright install chromium --with-deps   # one-time; needed by test_frontend.py
pytest tests/ -v
```

(`requirements-api.txt` is needed too since `tests/test_api.py` and
`tests/test_frontend.py` both import the API app.)

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
