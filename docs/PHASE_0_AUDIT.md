# Phase 0 — Foundation Audit

Repository: https://github.com/venusndk/DOCUMENTS-CONVERTER
Branch: `feature/phase-0-foundation-audit`
Date: 2026-09-05

This consolidates the Phase 0 deliverables (Current State Assessment,
Capability Report, Architecture Assessment, Target Architecture,
Capability Matrix, Regression Baseline, Risk Register, Dependency Map,
Phase Plan) into one document, as they describe one coherent snapshot at
this stage of the project. No platform code is built in this phase —
audit and planning only, per the engineering directive that requested it.

---

## 1. Current State Assessment

### Already working (verified by running the tool, not assumed)

| Capability | Status | Evidence |
|---|---|---|
| Scanned-vs-text PDF detection | Working | `is_scanned_pdf()`, checked via PyMuPDF text-layer sampling |
| Image input (`png/jpg/jpeg/tiff/tif/bmp`) | Working | `img2table.Image` wrapper, untested on a real image fixture in this audit (see Unverified) |
| OCR via Tesseract | Working | Verified across all runs this session |
| Bordered table detection | Working | `img2table` native |
| Borderless table detection | Working | `img2table` native, `borderless_tables=True` |
| Per-page bordered/borderless auto-selection | Working | `_pick_extraction_mode()`; confirmed necessary — one page needed borderless to be found at all, forcing borderless on another page fragmented a clean table |
| Grid-line-detection fallback | Working | `_manual_grid_table()` + `_detect_grid_lines()`; recovers pages `img2table` returns nothing usable for, and pages where it silently collapses columns |
| Rotated/vertical header OCR correction | Working | `_fix_rotated_cells()`; verified turning unreadable noise into correct course titles/codes |
| Cross-page header consensus correction | Working | `_consensus_correct_headers()`; verified correcting 526 header cells on a 35-page document. **Needs ≥3 pages sharing the same column count to activate** — did not fire on the current 8-page fixture, see Risk Register |
| Suspicious-cell flagging (allowlist-based) | Working | `_is_suspicious()` / `_write_table_flagged()`; verified catching every confirmed real OCR error found during manual testing, with no misses found |
| Excel formula-injection protection | Working | `_patched_workbook_init()` forces `strings_to_formulas=False`; verified 0 formula tags in raw XML across every regenerated file this session |
| CLI | Working | `argparse`-based, `-h` documents all flags accurately |

### Partially working

| Capability | Status | Detail |
|---|---|---|
| Column-collapse detection | Partial | Catches the case where `img2table`'s own OCR-refinement step drops whole columns *and total column count measurably shrinks*. Confirmed **not** to catch the narrower case where two adjacent cells merge mid-table without changing the table's overall column count (observed on Page 8 of the current fixture: `'None 80,50 None 78,50'` duplicated across two cells) — that case is currently only caught by the suspicious-cell flag, not corrected. |
| Numeric/name accuracy | Partial | High but not 100% in every sample checked (see `README.md` Accuracy & Trust Report) — isolated single-digit misreads (e.g. `78,52` vs `78,50`) occur and are, by nature, not catchable by any automated format check. |

### Broken

None currently known. Every bug found during development this session (formula-injection corruption, silent page loss, silent column collapse, a 1px crop-boundary sensitivity, a `.pages` lazy-init bug introduced during refactoring) was found, root-caused against real evidence, and fixed — see `scan_to_excel.py` inline comments for each, which document the failure mode and the fix.

### Missing

- **Automated test suite** — zero unit/integration/regression tests exist. All verification to date has been manual (render source page → compare against extracted cell). This is the single most important Phase 1 item if further refactoring is planned; see Phase Plan.
- **CI** — no GitHub Actions / other CI config.
- **Pinned dependency versions** — `requirements.txt` lists bare package names with no version pins (see Dependency Map). Builds are not currently reproducible.
- **License file** — none present; matters once this is a public GitHub repo.
- **Non-Windows testing** — every fix this session was verified on Windows only (`tesseract.exe` path handling, `PATH` manipulation for subprocess OCR calls are Windows-flavored, though should work cross-platform in principle).
- Everything in the directive's Phases 1–21 (web frontend, job queue, database, storage abstraction, auth, admin dashboard, AI vision fallback, batch processing, Docker, etc.) — none of it exists yet. This script is a single-process CLI tool only.

### Unverified

- Real-image (not PDF) input path — every test this session used PDF fixtures; the `Image` code path is exercised by `img2table` directly but hasn't been separately verified against a real photographed/scanned image file in this project.
- Behavior on a password-protected or corrupted PDF (no error-handling test performed for these).
- Behavior at scale (documents larger than the two tested: 35 pages/25MB and 8 pages/7MB).
- macOS/Linux compatibility (Tesseract/Poppler install paths, `PATH` handling).

---

## 2. Existing OCR → Excel Capability Report

Single capability, end to end:

```
scanned PDF / image
  → file-type detection (text-layer sniff)
  → per-page render (PyMuPDF, 200 DPI)
  → per-page bordered/borderless mode auto-selection
  → img2table table detection + Tesseract OCR
  → grid-line-detection fallback (classical CV) for pages img2table fails or truncates
  → rotated-header re-OCR (crop/rotate/upscale/re-read)
  → cross-page header consensus correction
  → suspicious-cell flagging
  → .xlsx (one worksheet per detected table, formula-injection-safe)
```

Verified end-to-end on two real documents (a 35-page and an 8-page
university transcript batch) with concrete before/after comparison
against the rendered source pages. Result quality is documented honestly
in `README.md`'s Accuracy & Trust Report — high but explicitly not
100%, with a working mechanism (yellow flags) for surfacing what isn't
certain.

---

## 3. Architecture Assessment (current)

**One file, `scan_to_excel.py` (952 lines), no package structure.**
Everything — CLI parsing, PDF rendering, table-detection orchestration,
OCR-quality fixes, consensus logic, validation, and xlsx writing — lives
in one module as a sequence of functions plus one dataclass
(`HighResPDF`). This is appropriate for what it currently is (a
single-purpose script one person runs from a terminal) and
**inappropriate** as the foundation for a multi-service platform without
restructuring — there are no seams (interfaces/providers) to plug a job
queue, a second OCR backend, or a web layer into today.

Monkeypatching is used deliberately in three places to fix bugs in the
`img2table`/`xlsxwriter` dependencies themselves (documented inline with
the exact failure mode each one fixes). This is a legitimate, narrow
technique here — but it means the code is coupled to the exact installed
versions of those libraries (see Dependency Map: unpinned versions are a
real risk specifically because of this).

---

## 4. Target Architecture (per the submitted directive, annotated)

The directive describes a full document-intelligence SaaS platform:
Next.js frontend, capability registry, pipeline orchestrator, provider
abstractions for OCR/conversion/storage, async job queue (Redis +
worker), Postgres, auth, admin dashboard, AI vision fallback, batch
processing, Docker/CI, 21 sequential phases each gated by a separate git
branch and manual PR review.

**Honest engineering assessment, since that was explicitly asked for
(directive §77, "Principal Architect" perspective):**

That target is internally consistent and not unreasonable *as a
description of what a real commercial document-conversion product looks
like*. It is also a multi-month, multi-person undertaking — the phase
list alone (§68) describes work on the scale of a small team's roadmap,
not something to execute unattended in a handful of sessions. Concretely:
Phase 14 alone (job queue + worker + retry/resume/cancel) is a
substantial distributed-systems project; Phase 28 (security hardening:
malicious-file testing, rate-limit bypass testing, IDOR testing) needs
dedicated security engineering; Phase 17 (auth + user workspace) is a
full account system.

**Recommendation:** treat the directive as the long-run vision, not the
immediate work plan. The right-sized near-term path (proposed Phase Plan
below) gets real value — a properly structured, tested core engine that
a future web layer or CLI can both sit on — without committing to
infrastructure (Postgres, Redis, cloud storage, auth) before there's a
concrete need for it. Building that infrastructure speculatively, before
Phase 5–9's core engine work is even test-covered, is the most likely way
for this project to stall.

---

## 5. Conversion Capability Matrix

Only one conversion exists today. Everything else in the directive's
format list (§17, §19) is aspirational.

| Source | Target | Status | Provider | Notes |
|---|---|---|---|---|
| Scanned PDF | XLSX | ✅ Working | `img2table` + Tesseract + custom fallback | Verified, see Accuracy report |
| Text-layer PDF | XLSX | ✅ Working (untested edge cases) | same | OCR runs but has little effect per code comment; not separately verified this session |
| Image (`png/jpg/tiff/bmp`) | XLSX | ⚠️ Unverified | `img2table.Image` | Code path exists, not exercised against a real image fixture in this audit |
| Everything else in §17/§19 | — | ❌ Not implemented | — | No code exists for any other source/target pair |

---

## 6. Regression Baseline

Captured by running the current tool against the development fixture
(8-page scanned transcript PDF, real student data — **not committed**,
see Risk Register #1) on 2026-09-05:

```
Input:              8-page scanned PDF, 200 DPI processing
Pages with output:  8 / 8            (100% page coverage)
Grid-fallback used: 5 pages (1, 2, 5, 6, 7)
Rotated-cell fixes: 131
Header consensus corrections: 0 (fixture too small/varied for the
                    ≥3-same-shape-page threshold to activate — expected
                    on an 8-page document with 4 distinct module-code
                    sections; verified activating correctly at 526
                    corrections on a 35-page single-institution document)
Suspicious cells flagged: 403
Formula-injection tags in output: 0
Runtime: not precisely timed this run; prior full runs on a 35-page/25MB
                    document completed in low single-digit minutes end
                    to end with `n_threads=2`.
```

Any future refactor of `scan_to_excel.py` should be checked against
these numbers on the same fixture before being trusted — a large swing
in any of them (especially page coverage or formula-tag count) is a
regression signal, not just noise, based on this session's experience of
each of those numbers being exactly what changed when a real bug was
introduced or fixed.

**This is a manual baseline, not an automated regression test.** Turning
it into one (pinned fixture + `pytest` assertions on these numbers) is
the top Phase 1 recommendation below.

---

## 7. Risk Register

| # | Risk | Severity | Mitigation status |
|---|---|---|---|
| 1 | **Real personal data (student names, ID numbers, grades) in local test fixtures.** These must never reach the GitHub repo, public or private, without explicit consent from a data controller at the institution. | High | Mitigated for now: `.gitignore` excludes `*.pdf`/`*.xlsx` at the repo root. **Not yet mitigated**: nothing stops someone from committing a fixture under `fixtures/` (which the `.gitignore` deliberately allows for future *synthetic* fixtures) with real data by mistake. Recommend a pre-commit check or clear team norm: fixtures must be fabricated data only. |
| 2 | No automated tests — every fix this session relied on manual before/after comparison. Safe for one developer in one long session; not safe for a team or for unattended agentic refactoring in later phases. | High | Not mitigated. Top Phase 1 item. |
| 3 | Unpinned dependency versions (`requirements.txt` has no version pins), combined with three deliberate monkeypatches of exact library internals (`img2table`, `xlsxwriter`). A routine `pip install --upgrade` could silently break the patches or change behavior the whole pipeline depends on. | Medium-High | Not mitigated. Pin exact versions now; the versions this was built/tested against are recorded in the Dependency Map below. |
| 4 | No OCR pipeline can guarantee 100% accuracy; isolated single-character misreads on well-formatted values are undetectable by any automated check. | Inherent, not fully mitigable | Partially mitigated by the flagging system (catches format-breaking errors) but explicitly not plausible-looking wrong digits. Documented honestly in README rather than hidden. |
| 5 | Directive's git workflow (branch-per-phase, PR-gated) assumes a persistent human reviewer between every phase. If phases are approved without the same scrutiny this Phase 0 audit received, quality will regress. | Medium | Process risk, not code risk — up to how strictly the merge gate (directive §72) is honored going forward. |
| 6 | Single-OS (Windows) verification only. | Low-Medium | Not mitigated; note for whoever runs this on Linux/macOS first. |

---

## 8. Dependency Map

Python: 3.14.4 (as run in this environment; no `python_requires` declared)

| Package | Installed version | Pinned in `requirements.txt`? |
|---|---|---|
| img2table | 2.0.0 | No |
| pytesseract | 0.3.13 | No |
| openpyxl | 3.1.5 | No |
| pdf2image | 1.17.0 | No (also effectively unused — `img2table` renders via `pypdfium2`/PyMuPDF directly, not `pdf2image`; kept for the `poppler` install-doc trail, worth revisiting) |
| PyMuPDF (`fitz`) | 1.27.2.3 | No |
| pillow | 11.3.0 | No |
| numpy | 2.3.5 | No (transitive, via img2table/opencv) |
| opencv-contrib-python | 5.0.0.93 | No (transitive, via img2table) |
| xlsxwriter | 3.2.9 | No (transitive, via img2table; directly monkeypatched — pin this one especially) |
| pypdfium2 | 5.13.0 | No (transitive) |
| beautifulsoup4 | 4.9.3 | No (transitive) |

System binaries (not pip-installable, both required):
- Tesseract OCR 5.5.3 — must be on `PATH` or passed via `--tesseract-cmd`
- Poppler 26.02.0 — installed for `pdf2image` per the setup docs; not
  currently on the hot path (see note above)

**Recommendation:** pin the top-level four (`img2table`, `pytesseract`,
`openpyxl`, `PyMuPDF`) to these exact versions immediately, since the
monkeypatches in `scan_to_excel.py` depend on their current internal
structure (e.g. `img2table.ocr._types.OCRData._group_words_by_parent`,
`xlsxwriter.Workbook.__init__`'s `options` handling). An upgrade should
be a deliberate, tested event, not something that happens silently on a
fresh `pip install`.

---

## 9. Phase Plan (proposed, right-sized)

The directive's Phase 0 instructions ask this audit to separate what's
real from what's aspirational and produce a plan — not to commit to all
21 of its phases verbatim. Proposed near-term sequence, each still small
enough to review in one sitting:

1. **Phase 1 — Test & package the existing engine.** Turn this audit's
   manual regression baseline into real `pytest` tests against a
   *synthetic* fixture (fabricated names/grades, not real student data).
   Restructure `scan_to_excel.py`'s functions into a small package
   (`documents_converter/ocr_excel/`) with the CLI as a thin wrapper —
   no behavior change, just seams to build on. Pin dependencies.
2. **Phase 2 — Provider boundary for OCR + table detection.** Wrap the
   existing Tesseract/img2table logic behind the `OCRProvider` /
   `TableDetectionProvider` interfaces the directive describes (§6, §10),
   with the current implementation as the only concrete provider. This
   is the seam a web layer or a second OCR backend would need later,
   built now while the logic is still fresh and test-covered.
3. **Phase 3 — Decide the actual product shape** before building web
   infrastructure: is this a local CLI tool distributed to individual
   staff, or a hosted multi-user service? That answer determines whether
   Phases 4+ (job queue, database, auth, admin dashboard) are needed at
   all yet, or whether a synchronous local tool with a thin GUI wrapper
   already meets the real need. Recommend deciding this explicitly
   before investing in Redis/Postgres/S3/auth infrastructure.

Phases beyond this point (web frontend, job queue, additional format
conversions, AI vision fallback, security hardening, deployment) should
be scoped once 1–3 are done and the product-shape question is answered,
rather than committed to now.

---

## Sign-off

Per the submitted directive's own rules (§70–72): this branch will be
pushed and this report delivered, then **no PR will be created, nothing
will be merged, and Phase 1 will not begin** until explicitly reviewed
and approved.
