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
    document_analysis.py             per-page digital/scanned/mixed classification,
                                      safe metadata, quality flags (Phase 4 completion)
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
        searchable_pdf.py             scanned document -> searchable PDF (Phase 5
                                      completion): original page image + invisible
                                      OCR text layer
        pdf_to_images.py              PDF -> one PNG per page, zipped (Phase 9)
        pdf_to_text.py                PDF -> plain text, OCR for scanned pages
        office_to_pdf.py              Word/Excel/PowerPoint -> PDF (LibreOffice)
        html_to_pdf.py                HTML -> PDF (LibreOffice)
        markdown_to_pdf.py            Markdown -> HTML -> PDF (LibreOffice)
        pdf_to_docx.py                 PDF -> DOCX (LibreOffice, Phase 10)
        pdf_to_pptx.py                 PDF -> PPTX (LibreOffice, Phase 10)
        _libreoffice.py               shared LibreOffice-headless conversion helper
    api/
        app.py                       minimal synchronous HTTP API (see below)
        config.py                    environment-based API configuration
        security.py                  magic-byte + decompression-bomb checks
        rate_limit.py                per-IP fixed-window rate limiter
        auth.py                      API-key authentication
        jobs.py                      database-backed async job store (Phase 1 completion)
        db.py                        SQLAlchemy engine/session setup (SQLite or Postgres)
        models.py                    ORM models (JobRecord)
        storage.py                   scratch-workspace allocation/cleanup (Phase 3 completion)
        audit.py                     structured audit trail (Phase 11)
        static/index.html            the web page -- upload, convert, download
migrations/                          Alembic migrations (Phase 1 completion)
alembic.ini
tests/
    conftest.py
    test_ocr_excel.py                 pipeline/provider regression tests
    test_registry.py                   capability registry unit tests
    test_api.py                        API tests
    test_audit.py                      audit trail + startup-guard unit tests
    test_jobs_db.py                    job store + migration unit tests
    test_storage.py                    storage abstraction unit tests
    test_document_analysis.py          document analysis unit tests
    test_searchable_pdf.py             searchable-PDF conversion unit tests
    test_pdf_conversions.py            PDF->images/text unit tests (Phase 9)
    test_libreoffice_conversions.py    Office/HTML/Markdown->PDF (Phase 9) and
                                      PDF->DOCX/PPTX (Phase 10) tests --
                                      @requires_libreoffice, skips locally, runs
                                      in Docker/CI
    test_frontend.py                   real-browser (Playwright) frontend tests
    fixtures/synthetic_scan.py        generates a fabricated (no real data) test PDF
    fixtures/synthetic_invoice.py     fabricated invoice-style table, saved as WEBP
                                      (Phase 8 completion: WEBP + a different table layout)
    fixtures/synthetic_office.py      fabricated .docx/.xlsx/.pptx/.html/.md fixtures
                                      (Phase 9; python-docx/python-pptx are dev-only,
                                      not part of the runtime conversion path)
docs/
    PHASE_0_AUDIT.md                  current-state audit, capability matrix, phase plan
.github/workflows/test.yml            CI: runs the test suite on every push/PR
Dockerfile                            containerizes the API (not the CLI), non-root + healthcheck
LICENSE                                MIT (Phase 11)
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
browser for a real, no-curl-required page: choose or drag a file, pick
what to convert it to, click Convert, and watch it process. An
OCR→Excel result shows the extracted tables for review first (Phase 7
completion, below) — every other target downloads automatically once
done. A single self-contained HTML file
(`documents_converter/api/static/index.html`, inline CSS/JS, no build
step) served directly by the API, calling the same `/api/v1/jobs`
endpoints documented below — nothing here has its own state or logic
beyond what those endpoints already provide and already have tests for.

Verified with a real headless browser (Playwright), not just by reading
the HTML: `tests/test_frontend.py` actually loads the page, picks a file
through the real file input, clicks the real button, and confirms a real
file downloads with correct data — the same standard as every other
end-to-end test in this project.

### Frontend capability discovery (Phase 2 completion)

The page fetches `GET /api/v1/capabilities` on load and uses it for two
things, so it never hardcodes what the registry already knows:

- the file input's `accept` attribute is the union of every registered
  capability's accepted extensions, not a fixed list maintained by hand;
- choosing a file populates a "Convert to" dropdown with every target
  format that file's extension is valid for (a `.png`, for instance,
  matches both the OCR→Excel and image→PDF capabilities, so both appear,
  defaulting to `xlsx` to match this page's original behavior), with the
  matching capability's own description shown underneath. A file with no
  matching capability at all is rejected client-side, before any network
  request, with the Convert button disabled.

If the capabilities fetch itself fails (network hiccup, or an older
server without the endpoint), the page falls back to its pre-Phase-2
behavior: accept anything, always send `target=xlsx`. Verified end to
end with a real browser: `tests/test_frontend.py` drives an actual image
through the dropdown to `target=pdf` and checks the downloaded file is a
real, correctly-sized PDF — not just that the dropdown renders.

### Status colors (this project's own numbering — see note below)

*This section's "Phase 10" predates switching to the master directive's
own phase numbers (used from "Core Conversion Engine" onward, below) —
it is not the same Phase 10 as "PDF → Office" further down.*

The page's one status display (`#status`) uses a centralized semantic
color system, defined once as CSS custom properties in `index.html` and
reused by three status classes (`.processing`, `.success`, `.error`) —
not hardcoded per state, and not named after what they look like:

| Meaning              | Class          | Color     |
|----------------------|----------------|-----------|
| Uploading/converting  | `.processing` | `#F59E0B` (amber) |
| Completed             | `.success`    | `#22C55E` (green) |
| Failed                | `.error`      | `#EF4444` (red)   |
| Idle/not started      | *(none — box is hidden)* | `#64748B` reserved for a future visible idle badge |

Color is never the only signal: `setStatus()` in the page's script
prepends a fixed icon per state (⟳ / ✓ / ✕) to the actual text, and
`#status` carries `role="status" aria-live="polite"` so a screen reader
announces the change too. Verified for WCAG AA contrast (≥4.5:1) in both
light mode and the page's `prefers-color-scheme: dark` variant — the page
had no dark theme before this phase; adding the semantic tokens as
light/dark pairs made supporting one straightforward, so the rest of the
page (background, card, borders, inputs) picked up a dark variant too.

Known limitation: this is the only status display in the app today,
so "consistent everywhere" currently means "consistent in the one place
it appears." A job-history list, per-file badges, or an admin dashboard
would reuse the same three classes/tokens rather than introduce new
colors, but none of those surfaces exist in this project yet.

## Format & capability registry

`documents_converter/registry.py` is the single source of truth for which
(source format → target format) conversions this service can perform, and
what actually performs each one — added so a new conversion plugs in
without hardcoding another special case into the API layer (`if ext ==
".pdf" and target == "xlsx": ... elif ...`, which only gets worse with
every conversion after the first). Both `/api/v1/convert` and
`/api/v1/jobs` route through it via an optional `target` field.

Ten capabilities are registered today:

| source format      | target            | accepts                                       | what it does                          |
|---------------------|-------------------|--------------------------------------------------|----------------------------------------|
| `scanned_document`   | `xlsx`            | `.pdf .png .jpg .jpeg .tiff .tif .bmp .webp` | the OCR + table-detection pipeline above |
| `image`              | `pdf`             | `.png .jpg .jpeg .tiff .tif .bmp .webp`      | plain image → single-page PDF, no OCR  |
| `scanned_document`   | `searchable_pdf`  | `.pdf .png .jpg .jpeg .tiff .tif .bmp .webp` | original page image + invisible OCR text layer (Phase 5 completion) |
| `pdf_document`       | `images`          | `.pdf`                                        | PDF → one PNG per page, zipped (Phase 9 completion) |
| `pdf_document`       | `text`            | `.pdf`                                        | PDF → plain text: native text layer where present, OCR where not |
| `office_document`    | `pdf`             | `.docx .doc .xlsx .xls .pptx .ppt`            | Word/Excel/PowerPoint → PDF, via LibreOffice headless |
| `html`               | `pdf`             | `.html .htm`                                  | HTML → PDF, via LibreOffice headless |
| `markdown`           | `pdf`             | `.md .markdown`                               | Markdown → HTML (the `markdown` package) → PDF |
| `pdf_document`       | `docx`            | `.pdf`                                        | PDF → DOCX, via LibreOffice headless (Phase 10 completion) |
| `pdf_document`       | `pptx`            | `.pdf`                                        | PDF → PPTX, via LibreOffice headless (Phase 10 completion) |

Deliberately no `pdf_document` → `xlsx`: that pair already exists as
`scanned_document` → `xlsx`, a genuinely better implementation (OCR +
purpose-built table detection) for that specific direction than a
generic LibreOffice import would be — registering a second one wouldn't
just be redundant, it would collide (both would claim `.pdf` for the
same target).

WEBP support (Phase 8 completion, master directive numbering) needed its
own magic-byte check (`documents_converter/api/security.py`): WEBP's
container has a 4-byte file size between `RIFF` and `WEBP` that varies
per file, so unlike every other format here it can't be one fixed
prefix — verified against a real file, not assumed from the spec.

### Core Conversion Engine (Phase 9 completion)

The five conversions above (`pdf_document`/`office_document`/`html`/
`markdown` → `images`/`text`/`pdf`) round out the master directive's
Phase 9 list. PDF→images and PDF→text need no new dependency (PyMuPDF
and, for scanned pages, the existing Tesseract pipeline already cover
them). Word/Excel/PowerPoint/HTML → PDF all go through **LibreOffice
headless** (`documents_converter/converters/_libreoffice.py`) — a
deliberate, informed choice, not a default reach: there's no lightweight
pure-Python option that renders real Office files correctly, so this
project accepts a real document-rendering engine (a system binary, not a
pip package) as the cost of that capability actually working. Markdown
routes through the same LibreOffice HTML path (via the `markdown`
package) rather than needing its own renderer.

Two LibreOffice quirks handled explicitly, not left to surprise the
first real user:
- It only lets you pick an output *directory*, not an exact output
  filename — each call runs against an isolated temp directory and the
  result is moved to the expected path.
- Multiple headless instances sharing the default user-profile directory
  fail against each other (a well-documented LibreOffice limitation).
  Since this service's job queue runs conversions concurrently
  (`_convert_executor`, up to 4 at once), every call gets its own
  throwaway profile directory — verified with a real test that runs 4
  LibreOffice conversions concurrently and checks all 4 succeed.

**Local dev note:** LibreOffice is not installed on this project's own
Windows dev machine — a deliberate choice, not an oversight (see the
Phase 9 commit message). `tests/test_libreoffice_conversions.py` and the
Office/HTML/Markdown tests in `tests/test_api.py` skip locally
(`@requires_libreoffice`) and run for real in Docker and CI, where it's
installed via `apt`.

**Known gap, disclosed rather than assumed away:** Office documents
(`.docx`/`.xlsx`/`.pptx`) are ZIP containers and can, like any ZIP-based
format, be zip-bombed — this project's decompression-bomb check
(`documents_converter/api/app.py::_check_decompression_bomb`) doesn't
cover them yet. Today's only protection is the raw upload size limit
(`MAX_UPLOAD_MB`) and the overall conversion timeout. Dedicated
malicious-file testing for these formats is master directive Phase 19's
job (Performance & Security Hardening), not this one's.

**Performance characteristic, measured not assumed:** each LibreOffice
conversion took roughly 6 seconds in real testing (4 concurrent HTML→PDF
jobs through a real running container, ~6.2–6.5s each) — headless
LibreOffice starts a fresh process per call rather than running as a
persistent daemon, so that per-call startup cost is paid every time.
Fine for the job-queue pattern this service already uses; a
higher-throughput deployment would want a persistent LibreOffice
listener (`--accept=socket,...`) instead, not built here since nothing
about this project's current scale needs it yet.

### PDF → Office (Phase 10 completion, master directive numbering)

PDF → DOCX and PDF → PPTX, same LibreOffice-headless engine as Phase 9,
just the reverse direction (`--convert-to docx`/`pptx` instead of
`pdf`). PDF → XLSX is deliberately **not** a new capability — see the
capability table above for why registering one would collide with the
existing, better `scanned_document` → `xlsx` pipeline.

Two real findings from this phase, found and fixed against an actual
running Docker container, not assumed to work like the PDF-producing
direction:
- LibreOffice's default PDF handling opens it as a **Draw** document,
  which has no docx/pptx export filter at all — it fails with "no
  export filter found" on stdout while still **exiting 0**. Converting
  *from* a PDF needs an explicit `--infilter=writer_pdf_import` (for
  docx) or `--infilter=impress_pdf_import` (for pptx) to force the PDF
  through Writer's/Impress's own import instead — and that flag must be
  one combined `--infilter=NAME` argument; passing the name as a
  separate argv entry is rejected outright.
- Quality is genuinely asymmetric with the PDF-producing direction:
  reconstructing an *editable* document from a fixed-layout PDF is
  best-effort. Confirmed against a real converted file: recovered text
  lands in a **floating text-box shape** anchored at the PDF's original
  coordinates (preserving visual layout), not a flowing body paragraph
  — so a plain `document.paragraphs` scan (python-docx) won't find it
  even though the text is genuinely there and editable; PPTX's
  equivalent (`shape.text_frame.text`, python-pptx) does see it
  directly. A complex layout, or a scanned/image-only PDF, degrades
  further toward embedding the original page images.

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
Confirmed for real when `searchable_pdf` (Phase 5 completion) was added:
the web page's target picker (Phase 2 completion, above) started
offering it automatically, with zero frontend code changes, because it
reads this same registry live instead of a hardcoded list.

The master directive's Phase 2 also names "format definitions" and a
separate "provider registry" alongside the capability registry and
capability matrix above. Deliberately not built as distinct
modules/abstractions here: every registered capability today has exactly
one implementation, so a provider layer that lets multiple providers
compete for the same capability would be pure structure with nothing yet
to plug into it -- exactly the premature infrastructure
`docs/PHASE_0_AUDIT.md` warns against. `Capability.source_format` /
`target_format` already serve as the format identifiers in practice (the
registry itself is the single place that could drift, and it can't,
since `/api/v1/capabilities` reads live from it). If a second provider
for the same capability becomes a real need (a cloud OCR fallback
alongside Tesseract, say), that's the point to introduce the
provider/capability split for real, not before.

### PDF Utilities (Phase 11 completion, master directive numbering)

*This is the master directive's Phase 11 ("PDF Utilities"). It is not
the same Phase 11 as this project's own, earlier production-readiness
hardening (LICENSE, non-root Docker user, audit trail, production auth
guard — see Security hardening below) — two different things share the
number because this project's own phase numbering predates switching to
the master directive's numbering.*

Eleven small, independent PDF operations, all pure PyMuPDF (no new
dependency, unlike Phases 9–10's LibreOffice work):
`documents_converter/pdf_utilities.py`. Exposed as their own family of
endpoints rather than through the capability registry/`/convert`
pipeline used everywhere else — a deliberate architectural choice, not
an oversight: the registry's model is one source format → one target
format via a single `target` string, and these operations don't fit
that shape (merge needs *multiple* input files; the rest need an
operation-specific parameter — a rotation angle, a page range, watermark
text — that a single `target` field has no room for). All 11 are
synchronous (no job-queue variant): every operation here is a fast,
local, in-process PyMuPDF manipulation with nothing to gain from Phase
7's async machinery, which exists for genuinely slow OCR/LibreOffice
work.

| Endpoint | Parameters | Returns |
|---|---|---|
| `POST /api/v1/pdf/merge` | `files` (2+ PDF uploads) | merged PDF, in upload order |
| `POST /api/v1/pdf/split` | `file`; optional `ranges` (e.g. `1-2;3-4`) | ZIP of PDFs — one page per file if `ranges` omitted |
| `POST /api/v1/pdf/extract` | `file`, `pages` (e.g. `3,1`) | PDF with just those pages, in the order given |
| `POST /api/v1/pdf/reorder` | `file`, `order` (must be a full permutation) | PDF with pages in that order |
| `POST /api/v1/pdf/delete-pages` | `file`, `pages` | PDF with those pages removed |
| `POST /api/v1/pdf/rotate` | `file`, `degrees` (multiple of 90), optional `pages` | PDF rotated (every page if `pages` omitted) |
| `POST /api/v1/pdf/watermark` | `file`, `text` | PDF with `text` stamped diagonally across every page |
| `POST /api/v1/pdf/add-page-numbers` | `file`, optional `start` (default 1) | PDF with a page number stamped bottom-center |
| `POST /api/v1/pdf/crop` | `file`, `left`/`top`/`right`/`bottom` (points) | PDF with each page's crop box shrunk inward |
| `POST /api/v1/pdf/compress` | `file` | PDF re-saved with structural cleanup + stream compression |
| `POST /api/v1/pdf/repair` | `file` | PDF re-saved through PyMuPDF's own (repairing) parser |

All require the API key like every other endpoint (`X-API-Key`, when
`API_KEYS` is configured — see Authentication below), reject non-`.pdf`
uploads before doing any real work, and log a `pdf_utility_requested`
audit event per call — same content-free logging discipline as the rest
of the service (operation name and outcome only, never the document
itself). `page` numbers in every parameter are 1-indexed for callers
(`pdf_utilities.parse_page_spec` converts to 0-indexed internally); a
page number out of range, a malformed spec, a non-permutation `order`,
a non-multiple-of-90 rotation, or crop margins that would consume the
whole page all return `400` rather than a stack trace or a silently
wrong result.

Three real bugs found and fixed in `add_watermark` during development,
each confirmed by rendering the actual output to an image and visually
inspecting it, not just by asserting the watermark text appears
somewhere in an extraction:
- PyMuPDF's `insert_textbox`/`insert_text` only accept `rotate` in
  multiples of 90 — the diagonal watermark needs an actual 45-degree
  angle, done instead via the `morph` parameter (a rotation matrix
  pivoting around a chosen point).
- `insert_textbox`'s rect-based layout anchors a line of text near the
  *top* of its box, not through its vertical center — after a 45-degree
  rotation around the page center, that mismatch swings the text toward
  a corner and off the physical page. Fixed by switching to `insert_text`
  with an explicitly computed origin point, so the rotation pivot and
  the text's own visual center coincide.
- The auto-shrink-to-fit font size was first budgeted against the
  page's corner-to-corner diagonal length, which still let long
  watermark text on a non-square page get clipped at both ends — a
  45-degree line from the center actually reaches the *shorter* of the
  page's width/height first, not the far corner, so the correct budget
  is `min(width, height) * sqrt(2)`, not the raw diagonal.

### Document Security (Phase 12 completion, master directive numbering)

Password protection, encryption, and permissions are one PyMuPDF
feature (`documents_converter/pdf_security.py`, no new dependency):
the PDF standard security handler, AES-256. Redaction is also pure
PyMuPDF, via its native redact-annotation mechanism — content within a
redacted region is genuinely removed from the file, not covered by a
black box drawn on top of it. Digital signing is a different kind of
problem: real, tamper-evident signing needs an actual cryptographic
signing library, not just a PDF-manipulation one, so this phase adds
**pyHanko** (a real, maintained PDF-signing/validation library) plus
`cryptography` (already one of pyHanko's own dependencies) — a
deliberate, disclosed dependency addition, the same kind of informed
cost/benefit call as Phase 9's LibreOffice.

| Endpoint | Parameters | Returns |
|---|---|---|
| `POST /api/v1/pdf/protect` | `file`, `user_password` and/or `owner_password`, optional `permissions` (comma-separated, e.g. `print,copy`) | PDF encrypted with AES-256 |
| `POST /api/v1/pdf/unlock` | `file`, `password` | PDF with encryption removed |
| `POST /api/v1/pdf/redact` | `file`, `terms` (comma-separated search strings) and/or `rects` (semicolon-separated `page,left,top,right,bottom`) | PDF with matched content permanently removed |
| `POST /api/v1/pdf/sign` | `file`, optional `reason`/`location`, optional `pkcs12_file` + `pkcs12_password` | PDF with a real digital signature added |
| `POST /api/v1/pdf/verify-signatures` | `file` | JSON report on every embedded signature |

Same conventions as Phase 11's PDF Utilities: `X-API-Key` when
`API_KEYS` is configured, `.pdf`-only input rejected before any real
work, and a `pdf_utility_requested`/`_completed`/`_failed` audit event
per call (operation name and outcome only — a password is never
written to the audit log, and never logged at all).

**Password protection / encryption / permissions.** At least one of
`user_password` (required to open the file) or `owner_password`
(required to change security settings or exceed the granted
permissions) is required — encryption with neither password set would
protect nothing. A `permissions` restriction only has teeth once
there's an owner password backing it (see `protect_pdf`'s docstring for
why); `POST /api/v1/pdf/unlock` reverses it given a password that
authenticates as either user or owner.

**Redaction.** `terms` does a case-sensitive search on every page via
PyMuPDF's own text search and redacts every match; `rects` targets
explicit regions regardless of their text content (useful for photos,
signatures, or a table cell). A `terms` search that matches nothing
raises rather than silently redacting zero occurrences — a caller
walking away believing "I redacted the SSN" when nothing was actually
removed is a real harm this project isn't willing to risk for a
security-sensitive operation.

**Signing — the trust model, stated plainly rather than left to be
assumed.** Without `pkcs12_file`, `/sign` uses this server process's own
demo signing identity (`pdf_security.generate_demo_signer`): a 2048-bit
RSA key and a self-signed certificate generated once in memory the
first time signing is requested, never written to disk, different every
time the process restarts. What the demo certificate does **not**
provide is identity: it has no chain to any trust root, so
`verify-signatures` always reports `trusted: false` for it, correctly —
there is no basis for a verifier to believe "Documents Converter Demo
Signer" refers to this deployment, let alone a real person or
organization. Real, identity-bound signing means supplying
`pkcs12_file` (a `.pfx`/`.p12` certificate+key bundle from a CA your
verifiers already trust) and `pkcs12_password` if it's encrypted.

What it **does** provide is genuine tamper-evidence, and getting that
disclosed correctly took a real bug fix, not just a docstring: the
first version of `/verify-signatures` only exposed pyHanko's combined
`trusted`/`bottom_line` verdict, which is *always* false for a
demo-signed file regardless of tampering (an untrusted signer alone
forces it) — so signing a file, tampering with its visible content
afterward via a legitimate PDF mechanism (an incremental update, the
same way a second signature would normally be added), and checking
`bottom_line` looked like tamper-evidence working, when in fact the
field couldn't distinguish "untrusted" from "untrusted AND tampered" at
all. Caught before shipping by testing the untampered case first and
finding `bottom_line` was already false there too. Fixed by adding a
dedicated `modified_after_signing` field, driven by pyHanko's
diff-analysis `modification_level` rather than its combined verdict —
confirmed for real against a live container: signing a file, calling
`/verify-signatures` (`modified_after_signing: false`), tampering with
it, and calling `/verify-signatures` again (`modified_after_signing:
true`, `trusted` unchanged at `false` throughout).

Signature verification is fully offline by design (no revocation/CRL/
OCSP fetching) — a verifier reaching out to a URL embedded in someone
else's uploaded file is its own risk this project isn't taking on; a
deployment that wants a specific CA's signatures to verify as trusted
would need to pass that CA's certificate as a trust root, not currently
exposed as an API parameter.

`POST /api/v1/pdf/verify-signatures` isn't one of the master directive's
five named Document Security sub-items (password protection,
encryption, redaction, signing, permissions) — added anyway as a
disclosed, natural complement: a signing endpoint with no way to check
what it produced would be a half-built feature, the same reasoning that
led Phase 7 to pair its preview endpoint with review-correction
endpoints.

### Batch Processing (Phase 13 completion, master directive numbering)

`POST /api/v1/batch` accepts multiple files under one shared `target`
and queues each as its own ordinary job on the exact same background
pipeline `POST /api/v1/jobs` already uses (`documents_converter/api/
batch.py`'s `BatchRecord` is deliberately just the list of job ids
submitted together, not a second job-execution system).

| Endpoint | Parameters | Returns |
|---|---|---|
| `POST /api/v1/batch` | `files` (2+ uploads), `target` | `202`, `{batch_id, total, job_ids}` |
| `GET /api/v1/batch/{id}` | — | overall `status`, per-status `counts`, and a per-file manifest |
| `GET /api/v1/batch/{id}/download` | — | one ZIP: every completed file's result (named by job id) + `manifest.json` |

**"Safe concurrency" means reuse, not a second pool.** A batch's files
are submitted to the exact same 4-worker `_convert_executor` every
other conversion already shares — a 50-file batch still only ever runs
4 conversions at once, the same limit a single caller already lives
under, so one large batch can't starve every other request the service
is handling.

**Individual failure reporting starts at submission, not just at the
end.** A file that fails validation (wrong extension for `target`, bad
magic bytes, a decompression-bomb page/pixel count) is recorded as its
own failed job immediately and does **not** block the rest of the
batch from being validated and queued — confirmed with a test that
submits one valid file and one that can't possibly satisfy `target` in
the same request: the valid file completes, the other reports `failed`
with its own error message, and the batch's overall status becomes
`completed_with_errors` (`queued`/`processing`/`completed`/`failed`
round out the possible values) — never a single bad file taking the
whole batch down. The downloaded ZIP's `manifest.json` carries the same
per-file status/error information, so a caller who only fetches the
ZIP still gets individual failure reporting, not just the successful
files with failures silently dropped.

`GET .../download` returns `409` while any file is still queued or
processing — the same "not ready yet" convention as
`GET /api/v1/jobs/{id}/result`.

### Job Queue & Real-Time Processing (Phase 14 completion, master directive numbering)

The biggest infrastructure change in this project so far: `/api/v1/jobs`
and `/api/v1/batch` no longer run conversions on an in-process thread
pool. They enqueue onto a real **Redis** queue (**RQ**), and a separate
**worker** process — its own container in `docker-compose.yml` — pulls
jobs off it and runs them. `docker run` one image is no longer enough
to demonstrate this for real; `docker compose up --build` brings up
Postgres, Redis, the API, and the worker together (Postgres, not the
default local SQLite, for the same reason this project has supported
both since Phase 1: two separate processes reading/writing the same
`JobRecord`/`BatchRecord` rows is exactly Postgres's job, not SQLite's,
once they're in genuinely separate containers rather than threads in
one process).

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/jobs/{id}/cancel` | Cancels a queued or in-flight job |
| `POST /api/v1/jobs/{id}/retry` | Manually re-enqueues a stuck/failed/cancelled job, reusing its already-saved input |
| `GET /api/v1/jobs/{id}/events` | Server-Sent Events: live status + progress as they change |
| `POST /api/v1/batch/{id}/resume` | Re-enqueues every non-completed file in a batch at once |

**Job lifecycle** gained a fifth status, `cancelled`, alongside the
existing `queued`/`processing`/`completed`/`failed`.

**Progress events** are Server-Sent Events, not WebSockets — this is
one-directional (server to caller) and needs no separate protocol
upgrade, which is all "watch one job's progress in real time" actually
needs. Progress text comes from the RQ job's own `meta` dict, updated
by the same `progress` callback every capability already accepts.

**Automatic retry** (RQ's own `Retry`) only ever applies to a
failure this project's own code can't already tell is permanent —
never a bad-input failure (wrong format, a decompression-bomb page
count), which would just fail identically again. Getting this
distinction to actually work took a real bug fix along the way: the
first version left a stale "will be retried" error message in place on
a job that failed once and then succeeded on retry, because the
success path never cleared it — caught by testing the retry-then-
succeed sequence for real (not just the eventually-fails case) and
finding the completed job still reported an error. A second, more
fundamental one followed: RQ's own automatic retry, given a nonzero
retry interval, *schedules* the retry rather than re-queueing it
immediately, and — confirmed by reading RQ's own source after a retry
sat stuck at "scheduled" forever in testing — nothing ever promotes a
scheduled job back onto the real queue unless the worker is run with
`with_scheduler=True`. Both `worker_main.py` and the test suite's own
background worker (`tests/conftest.py`) run with it enabled for exactly
this reason.

**Resumability** means a job or batch left stuck mid-conversion (a
worker that crashed or was killed) can be picked back up without
re-uploading anything — the same input file already sitting in the
job's `work_dir` is reused. Verified for real against a live
`docker compose` stack, not just unit tests: stopping the `worker`
container mid-queue and confirming a submitted job stays durably
`queued` in Redis (not lost); restarting the worker and watching it
pick the backlog straight back up; and, for the "worker died while
actively running a job" case specifically, forcing a completed job's
row back to `processing` directly in the real Postgres database (no
live RQ job behind it any more) and confirming `POST .../retry`
resumes it correctly. Finding this scenario also surfaced a real,
pre-existing bug unrelated to Phase 14 itself: `JobRecord.work_dir` was
being set on the in-memory `Job` object when a job was created but was
**never actually persisted to the database** — harmless as long as
nothing ever needed to look it up again later (which nothing did,
before resumability existed), but it also meant `JobStore`'s own
expired-job cleanup never once actually deleted a finished job's temp
directory, a real disk-space leak this phase's testing is what finally
surfaced. Fixed by persisting `work_dir` immediately in both
`create_job` and `create_batch`.

**The one thing a separate worker process needs that a single process
never did**: access to the exact same files the API process wrote.
`config.WORK_DIR_ROOT`, when set, points every job's scratch directory
at a shared location instead of each process's own OS temp directory —
`docker-compose.yml`'s `work_data` volume, mounted at the same path in
both the `api` and `worker` containers.

**Minimum Redis version: 4.0** (6+ recommended). RQ's own worker
registration issues a multi-field `HSET` — one field-value pair was all
`HSET` supported before Redis 4.0, and this project's own dev machine
actually surfaced the consequence for real: it has a native Windows
Redis installed, but it's version 3.0.504 (an unofficial, years-old
port), and every job submitted against it failed permanently with
`wrong number of arguments for 'hset' command` the moment a worker
tried to register itself — not a slow degradation, every single job
stuck at `queued` forever. `documents_converter/api/job_queue.py` also
forces RESP2 (`protocol=2`) rather than letting redis-py negotiate
RESP3 via `HELLO`, since that command doesn't exist before Redis 6.0
either and failed outright (`unknown command 'HELLO'`) against the same
old server — RESP2 works against both old and new Redis, so this is
strictly more compatible, not a downgrade for anyone already on a
modern one. `docker-compose.yml`'s `redis:7-alpine` and CI's service
container are both comfortably past this floor; a local native install
needs to be checked against it explicitly (`redis-cli INFO server`),
since "Redis is installed" alone doesn't mean "is new enough."

**Local dev note:** like Tesseract and LibreOffice before it, tests
that need a live Redis skip on this project's own dev machine
(`tests/conftest.py`'s `@requires_redis` — the machine's native Redis
being too old counts as "not usable" here too) — real verification
happens in Docker Compose and CI (a `redis:7-alpine` service container,
added to `.github/workflows/test.yml` for this phase — trivial to
provision there, unlike Tesseract/LibreOffice, so there was no reason
to leave these permanently skip-only). Tests that do run against a real
Redis use an in-process RQ worker thread (`tests/conftest.py`), not a
separate process — deliberately, so a test's own monkeypatched
conversion function (used to simulate a slow or flaky conversion) is
actually visible to the code that executes the job, which a genuinely
separate worker process could never see.

## Document analysis (Phase 4 completion)

`documents_converter/document_analysis.py` inspects a PDF or image and
reports, before any conversion runs:

- **Classification** — for PDFs, every page is checked for a real text
  layer (not just the first few, and not a whole-document guess): all
  digital pages → `digital`, all scanned → `scanned`, a genuine
  combination → `mixed`. Images are always `image` (a raster has no
  "native" text layer to check).
- **Safe metadata** — PDF producer/creator/creation-date/encrypted flag,
  or image format/mode/dimensions/DPI. Returned to the caller (it's
  their own file) but deliberately never written to the audit trail —
  these fields can occasionally carry a real person's name, and this
  project's logging policy (see Risk Register in `docs/PHASE_0_AUDIT.md`)
  is that server-side logs never carry anything read from inside a
  document.
- **Quality flags** — deliberately conservative: only conditions with a
  direct, mechanical link to a real problem (`no_pages`,
  `very_low_resolution`), not a tuned prediction of OCR accuracy. This
  project's own tested experience (see the `--dpi`/`--preprocess` note
  further down) is that plausible-sounding image-quality heuristics did
  not actually predict OCR outcomes on the one real document tested
  against — an elaborate, unvalidated quality *score* would repeat that
  mistake, not fix it.

Exposed via `POST /api/v1/analyze` (see below) — independent of
`/convert`/`/jobs`, so a caller (or a future frontend feature) can
inspect a file before committing to a conversion.

## Preview & human review (Phase 7 completion)

An OCR→Excel job's completed result isn't just a file to download
blind: `GET /api/v1/jobs/{id}/review` returns every detected table as
structured JSON — one entry per cell, with its value and whether it
tripped the suspicious-cell check (the same `--flag-suspicious-cells`
check that highlights cells yellow in the `.xlsx` itself) — and
`POST /api/v1/jobs/{id}/review` accepts corrections, applied to both
that JSON and the actual result file, so a later download reflects
them. A corrected cell that no longer looks suspicious has its
highlight cleared automatically. Only OCR→Excel jobs produce review
data (`GET /api/v1/jobs/{id}`'s `has_review` field says so) — there's
no "table" concept to review for the image→PDF or searchable-PDF
targets.

The web page uses this directly: after a job with `has_review: true`
completes, it shows the extracted tables inline instead of downloading
immediately — flagged cells marked ⚠ (never color alone, per the Phase
10 accessibility standard), every cell directly editable in place — and
only downloads once "Confirm & Download" is clicked, saving whatever
was edited first. Verified with a real browser (Playwright): edited an
actual cell through the real UI, confirmed, and checked the downloaded
`.xlsx` reflects the edit.

```powershell
curl.exe http://127.0.0.1:8000/api/v1/jobs/a1b2c3.../review
curl.exe -X POST http://127.0.0.1:8000/api/v1/jobs/a1b2c3.../review `
  -H "Content-Type: application/json" `
  -d '[{"sheet_name": "Page 1 - Table 1", "row_index": 0, "col_index": 0, "value": "Corrected"}]'
```

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

The job store persists to a real database (`documents_converter/api/db.py`,
`jobs.py`; Phase 1 completion, master directive numbering) instead of an
in-memory dict — jobs now survive a restart, and, with a shared Postgres
instance, are visible across more than one replica. `DATABASE_URL`
defaults to a local SQLite file (`./data/documents_converter.db`), so the
command above needs no extra setup; point it at Postgres instead for that:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://user:pass@localhost:5432/documents_converter"
```

Migrations (Alembic, `alembic.ini` + `migrations/`) run automatically at
startup against whichever `DATABASE_URL` is configured — no separate
manual step for local/dev use. To run them by hand instead (e.g. as an
explicit release step before rolling out a change to more than one
replica, so they don't race each other migrating on boot):

```powershell
alembic upgrade head
```

Verified against both backends for real, not just assumed compatible via
the ORM: ran the actual migration, started the real API, submitted a job,
killed the process, and confirmed a **brand-new process** could still see
the completed job — against a real local SQLite file and, separately,
against a real disposable Postgres container.

```
GET  /health                    -> {"status": "ok", "tesseract_available": true}
GET  /api/v1/capabilities       -> what conversions are registered (see above)

POST /api/v1/analyze            -> upload a file, get back its page-level digital/
                                    scanned/mixed classification, safe metadata, and
                                    quality flags -- no conversion runs. See below.

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
                                    completed|failed", "has_review": bool,
                                    "error": "..." (if failed)}
GET  /api/v1/jobs/{id}/result    -> the finished file, once status is "completed"
                                    (409 otherwise) -- Content-Type and filename
                                    extension match whichever capability ran.
GET  /api/v1/jobs/{id}/review    -> extracted table data + suspicious flags, once
                                    "has_review" is true (404 otherwise -- only
                                    OCR->Excel jobs produce this). See below.
POST /api/v1/jobs/{id}/review    -> apply corrections (see below); updates both the
                                    review data and the actual result file.
```

Analyze example (a genuinely mixed PDF -- one digital page, one scanned):
```powershell
curl.exe -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/analyze
```
```json
{
  "doc_type": "pdf", "page_count": 2, "classification": "mixed",
  "pages": [
    {"index": 0, "has_text_layer": true, "width_pt": 300.0, "height_pt": 200.0},
    {"index": 1, "has_text_layer": false, "width_pt": 300.0, "height_pt": 200.0}
  ],
  "metadata": {"producer": null, "creator": null, "creation_date": null, "encrypted": false},
  "quality_flags": []
}
```

Synchronous example (defaults to the OCR->Excel capability):
```powershell
curl.exe -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o result.xlsx
```

Synchronous example, a different target (the image->pdf capability):
```powershell
curl.exe -F "target=pdf" -F "file=@photo.png" http://127.0.0.1:8000/api/v1/convert -o result.pdf
```

Synchronous example, searchable PDF (Phase 5 completion — same scanned
document, now selectable/searchable/copyable text instead of a table):
```powershell
curl.exe -F "target=searchable_pdf" -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o result.pdf
```

Synchronous examples, Phase 9 completion (Core Conversion Engine):
```powershell
curl.exe -F "target=images" -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o pages.zip
curl.exe -F "target=text" -F "file=@transcript.pdf" http://127.0.0.1:8000/api/v1/convert -o transcript.txt
curl.exe -F "target=pdf" -F "file=@report.docx" http://127.0.0.1:8000/api/v1/convert -o report.pdf
curl.exe -F "target=pdf" -F "file=@page.html" http://127.0.0.1:8000/api/v1/convert -o page.pdf
curl.exe -F "target=pdf" -F "file=@notes.md" http://127.0.0.1:8000/api/v1/convert -o notes.pdf
```

Synchronous examples, Phase 10 completion (PDF → Office):
```powershell
curl.exe -F "target=docx" -F "file=@report.pdf" http://127.0.0.1:8000/api/v1/convert -o report.docx
curl.exe -F "target=pptx" -F "file=@report.pdf" http://127.0.0.1:8000/api/v1/convert -o report.pptx
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

The job store (`documents_converter/api/jobs.py`) is database-backed as
of Phase 1 completion (see above) -- jobs survive a restart, and with a
shared Postgres instance are visible across multiple replicas too. The
worker pool that actually runs conversions (`_convert_executor`) is still
in-process, though, so multiple replicas still each need their own; only
the job *records* (status, result location) are shared. Finished jobs'
files are cleaned up after `JOB_RETENTION_SECONDS` (default: 1 hour).

Configuration (environment variables, see `documents_converter/api/config.py`):
`TESSERACT_CMD` (default: none, i.e. must be on `PATH`), `MAX_UPLOAD_MB`
(default: 50), `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`
(default: 10 requests per 60s per client IP), `CONVERT_TIMEOUT_SECONDS`
(default: 180), `JOB_RETENTION_SECONDS` (default: 3600), `DATABASE_URL`
(default: local SQLite), `API_KEYS`
(default: empty, i.e. auth off — see below), `ENVIRONMENT` and
`AUDIT_LOG_PATH` (Phase 11, see Security hardening below).

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

**Production startup guard (Phase 11):** set `ENVIRONMENT=production` and
the app refuses to start at all if `API_KEYS` is still empty — a real
hosted deployment shouldn't be able to go live unauthenticated just
because someone forgot to set a key. `ENVIRONMENT` defaults to
`development`, where this check never blocks startup, so a fresh local
checkout keeps working with zero configuration. Verified against a real
container: `ENVIRONMENT=production` with no `API_KEYS` set exits
immediately (checked its actual exit code, not just the log output);
the same environment with a key set starts normally and enforces it.

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
- **Storage abstraction** (master directive Phase 3 completion,
  `documents_converter/api/storage.py`) — every scratch directory an
  upload, a synchronous conversion, or an async job reads and writes
  real files in is now allocated/released through one seam instead of
  `tempfile`/`shutil` calls scattered across `app.py` and `jobs.py`.
  Same on-disk behavior as before (one backend, `LocalDiskStorage`,
  ships today) — the point is a future backend (e.g. one that also
  syncs to S3, for results that need to outlive a single container)
  can be swapped in behind this one interface instead of touching every
  call site.
- **Production startup guard** (Phase 11) — see Authentication above.
- **Audit trail** (Phase 11, `documents_converter/api/audit.py`) — every
  conversion attempt (`/convert` and `/jobs`) writes a structured JSON-line
  record: timestamp, request/job id, source extension, target format,
  size, client IP, whether auth was enforced, and success/failure with a
  reason category and duration. Always to stdout; also to a file if
  `AUDIT_LOG_PATH` is set (e.g. on a mounted volume), for a deployment
  that wants that record to outlive the container without a database.
  Never the document's content or client-supplied filename, same policy
  as every other log line in this project. This exists specifically
  because the README elsewhere describes this service handling "trusted
  official documents" — that claim needs *some* durable record of who
  converted what and when, not just ephemeral debug prints.

Combined with the Authentication section above, the API is now closer to
suitable for untrusted traffic — the main remaining gap is the rate
limiter's single-process limitation noted there, which matters once this
runs as more than one replica.

### Running the API in Docker

```powershell
docker build -t documents-converter-api .
docker run -p 8000:8000 documents-converter-api
```

Tesseract and LibreOffice (Phase 9 completion — `libreoffice-writer`/
`-calc`/`-impress`, not the full suite) are both installed inside the
image, so no `TESSERACT_CMD`/`LIBREOFFICE_CMD` is needed there. Verified
locally: built the image, ran it, and confirmed a real conversion
through the container produces the same correct data as running
natively — worth knowing if you compare outputs closely, the
*blank*-cell noise can differ slightly page to page, since the Linux
`apt` Tesseract build reads faint empty-cell artifacts a little
differently than the Windows build used elsewhere in this project;
that's cosmetic, not a correctness issue (see the Accuracy & trust report
below on this class of noise generally).

The container runs as an unprivileged user (`appuser`, Phase 11) rather
than root — confirmed with `docker exec ... whoami` against a real running
container, not just read from the Dockerfile — and declares a
`HEALTHCHECK` against `/health` so an orchestrator can detect a wedged
container, not only a crashed one.

The default `DATABASE_URL` (local SQLite at `/app/data/documents_converter.db`
inside the container) lives on the container's own writable layer, which
is discarded when the container is removed -- mount a volume at `/app/data`
if you want that file to survive `docker rm`, or set `DATABASE_URL` to a
real Postgres instance instead:

```powershell
docker run -p 8000:8000 -v documents-converter-data:/app/data documents-converter-api
# or:
docker run -p 8000:8000 -e DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db" documents-converter-api
```

A single `docker run` still works for the synchronous `/api/v1/convert`
endpoint and for exercising most of the API, but `/api/v1/jobs` and
`/api/v1/batch` need a real Redis and a real worker process behind them
as of Phase 14 (master directive numbering) — see that phase's own
section above for what they do. For the real thing:

```powershell
docker compose up --build
# port 8000 already taken by something else? ->
API_PORT=8001 docker compose up --build
```

Brings up Postgres, Redis, the API, and a worker together — see
`docker-compose.yml`'s own comments for why Postgres (not the default
local SQLite) and a shared volume for job scratch directories are both
load-bearing here, not incidental choices. Scale worker capacity
independently of the API process with `docker compose up --build
--scale worker=3`.

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
they're regressions this project actually hit during development. Three
things this project depends on aren't installed on its own dev machine --
Tesseract, LibreOffice, and (Phase 14, master directive numbering) Redis
-- and every test that needs one skips automatically
(`@requires_tesseract`/`@requires_libreoffice`/`@requires_redis` in
`tests/conftest.py`) rather than failing; all three run for real in
Docker/Docker Compose and CI, where they're actually provisioned.

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

### A new failure mode found and fixed (Phase 8 completion)

Testing against a genuinely different table layout (a fabricated
invoice-style item/quantity/price/total table, built specifically
because every other test document in this project used the same
transcript-style layout) found a real bug: a short value (e.g. a
single-digit quantity) positioned close to its cell's border could come
back **completely empty** from img2table's own per-cell OCR pass — not
misread, silently missing. Root-caused directly (identical crop, re-OCR'd
with and without trimming a few pixels from each edge) to the same class
of bug already fixed once in this project for rotated header cells:
border/gridline pixels bleeding into the OCR crop. Fixed the same way —
`_fix_empty_cells` (`documents_converter/ocr_excel.py`) re-crops any
cell that came back empty with a small inward margin and re-OCRs it,
touching only cells that were already empty so an already-correct short
read (like `"M"` for Sex) is never put at risk. `--no-empty-cell-fix` to
disable. This is the value of testing against more than one document
layout — the transcript fixture's short values never happened to sit
close enough to a border to trigger this.

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
   The web page and `GET/POST /api/v1/jobs/{id}/review` (Phase 7
   completion, above) make that recommended review step an actual part
   of the workflow, not something left to a separate manual step outside
   this tool.

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

## License

[MIT](LICENSE) — chosen as a standard, permissive default (Phase 11);
swap it for something else if your institution needs different terms
before this is exposed publicly.
