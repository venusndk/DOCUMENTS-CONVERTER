"""
HTTP API around convert_scanned_to_excel.

Phase 3 built a synchronous endpoint (POST /api/v1/convert): the
conversion runs inline and the response holds the connection open until
it's done. That's still here, unchanged, for callers who want it -- small
files convert quickly enough that a job/poll round trip is needless
overhead for them.

Phase 7 added a genuine async path (POST /api/v1/jobs, GET
/api/v1/jobs/{id}, GET /api/v1/jobs/{id}/result) for callers who don't
want to hold a connection open for a slow OCR run: submit returns
immediately with a job id, the actual conversion runs in the background,
and the caller polls for status. See jobs.py for the job store -- Phase 1
completion (master directive numbering) moved it from an in-memory dict
to a real database (db.py, models.py, migrations/), so jobs now survive
a restart. Both paths still share the same validation
(_validate_and_save_upload, _check_decompression_bomb) and the same
worker pool (_convert_executor) -- documented as a known limitation in
docs/PHASE_0_AUDIT.md: heavy async job load could delay sync requests,
since they compete for the same 4 worker threads, and that pool itself
is still per-process (only the job *records* are now shared/durable).

Phase 4 added security hardening on top of Phase 3's basic hygiene
(extension allowlist, safe temp-file naming, no document-content
logging): magic-byte validation, decompression-bomb limits, per-IP rate
limiting, a best-effort conversion timeout, and a catch-all exception
handler so nothing unexpected ever leaks a stack trace to the caller.
Phase 3 completion (master directive numbering) later centralized the
raw tempfile.mkdtemp()/TemporaryDirectory() and shutil.rmtree() calls
that used to be scattered across this file behind one storage
abstraction (storage.py) instead -- same on-disk behavior, now one seam
a future backend could plug into rather than several call sites to
update.

Phase 6 added API-key authentication on every /api/v1/* route (see
auth.py) -- disabled by default until config.API_KEYS is set, so
local/dev use needs no extra setup. /health stays unauthenticated on
purpose: load balancers and monitoring probes need to reach it without
credentials.

Phase 9 (docs/PHASE_0_AUDIT.md numbering continued) generalized both
/api/v1/convert and /api/v1/jobs from "always OCR->Excel" to "whatever
the capability registry (documents_converter/registry.py) knows how to
do", routed by an optional `target` field that defaults to "xlsx" so
every existing caller keeps working unchanged. See GET
/api/v1/capabilities for what's registered.

Phase 11 added two production-readiness pieces: a startup guard
(_check_startup_config) that refuses to run unauthenticated in
ENVIRONMENT=production, and a structured audit trail (audit.py) recording
who converted what kind of file to what target and whether it succeeded
-- separate from the ad-hoc print() logging already used for request
diagnostics throughout this file.

Phase 4 completion (master directive numbering) added POST
/api/v1/analyze: inspects an uploaded file (document_analysis.py) and
reports page-level digital/scanned/mixed classification, safe metadata,
and quality flags -- without running any conversion. Independent of the
`/convert` and `/jobs` paths above; a caller can inspect a file before
committing to a full (and, for OCR targets, slower) conversion job.

Run locally:
    uvicorn documents_converter.api.app:app --reload
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from pathlib import Path

import openpyxl
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from openpyxl.styles import PatternFill
from PIL import Image as PILImage

import fitz

from . import audit, config, security
from .auth import require_api_key
from .db import check_db_connection
from .jobs import Job, JobStore
from .rate_limit import FixedWindowRateLimiter
from .storage import storage
from .. import converters  # noqa: F401 -- import for its registration side effect only
from ..document_analysis import analyze
from ..ocr_excel import _is_suspicious, check_tesseract_available
from ..registry import Capability, registry


def _run_migrations() -> None:
    """
    Applies any pending Alembic migrations against config.DATABASE_URL at
    startup, so a fresh checkout (or a fresh container against a fresh
    database) doesn't need a separate manual `alembic upgrade head` step
    to become usable -- consistent with this project's running "zero
    extra setup" bar for local/dev use.

    Idempotent (upgrading an already-current database is a no-op), which
    matters here since this runs every time the process starts, not just
    once. For a deployment that runs multiple replicas against the same
    database, running migrations as an explicit separate release step
    instead (skip calling this, run `alembic upgrade head` once before
    rolling out) avoids every replica racing to migrate on boot --
    tracked as a known simplification for this single-instance-shaped
    project, not silently assumed away.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    repo_root = Path(__file__).resolve().parents[2]
    cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    command.upgrade(cfg, "head")


def _check_startup_config() -> None:
    """
    Fails fast if this process is about to serve /api/v1/* unauthenticated
    in production (config.ENVIRONMENT == "production" and no API_KEYS
    configured) -- a real hosted deployment of this service should not be
    able to go live open-to-anyone by omission. Does nothing in the
    "development" default, so a fresh local checkout keeps working with
    zero configuration.

    A plain function, not inlined in the lifespan handler below, so it's
    directly unit-testable (documents_converter.api.app._check_startup_config)
    without depending on how faithfully a given ASGI test client emulates
    lifespan events.
    """
    if config.ENVIRONMENT == "production" and not config.API_KEYS:
        raise RuntimeError(
            "ENVIRONMENT=production but API_KEYS is not set -- refusing to start "
            "unauthenticated in production. Set API_KEYS (see config.py), or set "
            "ENVIRONMENT=development for local/trusted-network use only."
        )
    if not config.API_KEYS:
        print(
            "[startup] WARNING: API_KEYS is not set -- every /api/v1/* route is "
            "unauthenticated. Fine for local development; set API_KEYS before "
            "exposing this service beyond a trusted network."
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _run_migrations()
    _check_startup_config()
    yield


app = FastAPI(title="Documents Converter API", version="0.1.0", lifespan=_lifespan)

_rate_limiter = FixedWindowRateLimiter(
    max_requests=config.RATE_LIMIT_MAX_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
)
_job_store = JobStore(retention_seconds=config.JOB_RETENTION_SECONDS)
# Shared by both the sync endpoint and the async job runner -- see module
# docstring for the resulting known limitation. Also what actually gives
# the sync endpoint a wall-clock timeout ("best-effort" because Python has
# no safe API to force-kill a thread; an abandoned one keeps running until
# it finishes on its own, it's just no longer waited on).
_convert_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="convert")

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    """
    Phase 8 (docs/PHASE_0_AUDIT.md): a real, usable page for the API this
    project spent seven phases hardening -- until now, using it meant
    writing curl commands. Deliberately a single self-contained HTML file
    with inline CSS/JS, served directly rather than through a separate
    frontend build/deploy pipeline: it calls the same /api/v1/jobs
    endpoints any other client would, with no server-side templating or
    extra state, so there's nothing here that needs its own test
    infrastructure beyond "does it load and does it work in a real
    browser" -- see tests/test_frontend.py.
    """
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict:
    """
    Liveness/readiness check. Reports whether Tesseract is actually
    reachable (not just that the process is up) -- a health check that
    only proves the web server started is not very useful for an OCR
    service whose real dependency is an external binary. Phase 1
    completion added a real database round trip alongside it, for the
    same reason: the job store now depends on it being reachable.
    """
    return {
        "status": "ok",
        "tesseract_available": check_tesseract_available(config.TESSERACT_CMD),
        "database_available": check_db_connection(),
    }


@app.get("/api/v1/capabilities")
def list_capabilities() -> list[dict]:
    """
    Discovery endpoint over the capability registry (Phase 9,
    documents_converter/registry.py): what conversions this server can
    perform right now, and which file extensions/target format each one
    accepts. Unauthenticated like /health -- it's metadata about the
    service, not a document operation, so it carries no more risk than
    reading the API docs.
    """
    return [
        {
            "source_format": c.source_format,
            "target_format": c.target_format,
            "description": c.description,
            "accepted_extensions": sorted(c.source_extensions),
        }
        for c in registry.list_all()
    ]


def _resolve_capability(ext: str, target: str) -> Capability:
    """Shared by both /convert and /jobs: turns (uploaded file's
    extension, requested target format) into the Capability that
    handles it, or a clear 400 if nothing matches. This is what replaced
    the old hardcoded `ext not in config.ALLOWED_EXTENSIONS` check --
    "allowed" is now whatever the registry says it can route, not a
    single global list."""
    try:
        capability = registry.find(target, ext)
    except ValueError as e:
        # Ambiguous registration -- a bug in this service's own setup,
        # not the caller's fault, but still safer to report as a clean
        # error than to 500.
        raise HTTPException(status_code=500, detail=str(e)) from e
    if capability is None:
        known_targets = sorted({c.target_format for c in registry.list_all()})
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}' for target '{target}'. "
            f"Known target formats: {known_targets}. See GET /api/v1/capabilities.",
        )
    return capability


def _client_ip(request: Request) -> str:
    """Shared by rate limiting and the audit log below -- the closest
    thing to "who" this service can identify today, since there's no
    user-account system yet (see auth.py's own docstring)."""
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> None:
    if not _rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")


def _validate_and_save_upload(request: Request, file: UploadFile, work_dir: Path) -> tuple[Path, str]:
    """
    Shared by both the sync and async endpoints: validates upload size and
    magic-byte signature while streaming the upload to `work_dir`. Raises
    HTTPException on any validation failure.

    Does NOT gate on a fixed extension allowlist -- since Phase 9
    (documents_converter/registry.py) that job belongs to
    _resolve_capability, called separately by each endpoint once it also
    knows the requested target format. This function only extracts the
    extension and checks that the file's actual bytes match what it
    claims to be.
    :return: (input_path, ext)
    """
    ext = Path(file.filename or "").suffix.lower()

    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    # Cheap early rejection before reading any of the body. Content-Length
    # reflects the whole multipart payload (a little larger than the file
    # alone), so the per-chunk check below remains the authoritative limit
    # -- this is just to avoid doing any work at all for an obviously
    # oversized request.
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"File exceeds the {config.MAX_UPLOAD_MB} MB limit."
        )

    # Never trust the client-supplied filename for path construction --
    # write under a fixed, safe name instead.
    input_path = work_dir / f"input{ext}"
    size = 0
    header_checked = False
    with open(input_path, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            if not header_checked:
                if not security.matches_magic_bytes(ext, chunk[: security.MAGIC_BYTES_TO_READ]):
                    raise HTTPException(
                        status_code=400,
                        detail=f"File content doesn't match its extension ({ext}).",
                    )
                header_checked = True
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds the {config.MAX_UPLOAD_MB} MB limit.",
                )
            f.write(chunk)

    return input_path, ext


def _check_decompression_bomb(input_path: Path, ext: str) -> None:
    """Checks the parsed/decompressed size (PDF page count, image pixel
    dimensions) before the expensive OCR pipeline runs. Raises
    security.FileTooLargeError if it's over the configured limit."""
    if ext == ".pdf":
        doc = fitz.open(str(input_path))
        n_pages = len(doc)
        doc.close()
        security.check_pdf_page_count(n_pages)
    else:
        with PILImage.open(input_path) as img:
            security.check_image_dimensions(*img.size)


@app.post("/api/v1/analyze", dependencies=[Depends(require_api_key)])
def analyze_document(request: Request, file: UploadFile) -> dict:
    """
    Inspects an uploaded PDF or image and reports its page-level
    digital/scanned/mixed classification, safe metadata, and quality
    flags (documents_converter/document_analysis.py) -- without running
    any conversion. Lets a caller decide what to do with a file, or show
    a preview of what a conversion would be working with, before
    committing to the (for OCR targets, much slower) /convert or /jobs
    endpoints. Gated by the same auth/rate-limit policy as those, since
    it still processes a real uploaded file.
    """
    _check_rate_limit(request)

    work_dir = storage.allocate("docconv-analyze-")
    try:
        input_path, ext = _validate_and_save_upload(request, file, work_dir)
        if ext not in security.MAGIC_SIGNATURES:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot analyze '{ext}' -- not a recognized PDF/image extension.",
            )

        request_id = uuid.uuid4().hex[:12]
        size_bytes = input_path.stat().st_size
        audit.log_event(
            "analyze_requested",
            request_id=request_id,
            ext=ext,
            size_bytes=size_bytes,
            client_ip=_client_ip(request),
            auth_enforced=bool(config.API_KEYS),
        )

        try:
            _check_decompression_bomb(input_path, ext)
            result = analyze(input_path, ext)
        except security.FileTooLargeError as e:
            audit.log_event("analyze_failed", request_id=request_id, reason="file_too_large")
            raise HTTPException(status_code=413, detail=str(e)) from e
        except Exception as e:
            # Per docs/PHASE_0_AUDIT.md failure philosophy: never expose a
            # raw stack trace to the caller.
            print(f"[{request_id}] analysis failed: {e!r}")
            audit.log_event("analyze_failed", request_id=request_id, reason="internal_error")
            raise HTTPException(
                status_code=500,
                detail="Analysis failed. This has been logged for investigation.",
            ) from e

        # Safe, derived fields only (category names, counts) -- never the
        # metadata dict itself, which is returned to the caller below but
        # deliberately kept out of server-side logs (see
        # document_analysis.DocumentAnalysis's own docstring).
        audit.log_event(
            "analyze_completed",
            request_id=request_id,
            doc_type=result.doc_type,
            classification=result.classification,
            page_count=result.page_count,
            quality_flags=result.quality_flags,
        )
        return {
            "doc_type": result.doc_type,
            "page_count": result.page_count,
            "classification": result.classification,
            "pages": [
                {
                    "index": p.index,
                    "has_text_layer": p.has_text_layer,
                    "width_pt": p.width_pt,
                    "height_pt": p.height_pt,
                }
                for p in result.pages
            ],
            "metadata": result.metadata,
            "quality_flags": result.quality_flags,
        }
    finally:
        storage.release(work_dir)


@app.post("/api/v1/convert", dependencies=[Depends(require_api_key)])
def convert(request: Request, file: UploadFile, target: str = Form("xlsx")) -> Response:
    """
    Accepts one file and returns the converted result. `target` selects
    which registered capability handles it (documents_converter/registry.py)
    and defaults to "xlsx" -- the original scanned-document-to-Excel
    pipeline every caller before Phase 9 already relies on -- so existing
    callers that never send `target` keep getting exactly the same
    behavior. Synchronous: the response is the finished file, not a job
    reference. See /api/v1/jobs for the async alternative, and GET
    /api/v1/capabilities for what `target` values are available.
    """
    _check_rate_limit(request)

    work_dir = storage.allocate("docconv-")
    try:
        input_path, ext = _validate_and_save_upload(request, file, work_dir)
        capability = _resolve_capability(ext, target)
        output_path = work_dir / f"output{capability.output_extension}"

        request_id = uuid.uuid4().hex[:12]
        size_bytes = input_path.stat().st_size
        # Safe metadata only (docs/PHASE_0_AUDIT.md: never log document
        # contents) -- extension and size, not the client-supplied filename
        # or anything read from inside the file.
        print(
            f"[{request_id}] convert request: ext={ext} target={target} "
            f"size={size_bytes}B"
        )
        audit.log_event(
            "convert_requested",
            request_id=request_id,
            endpoint="sync",
            source_format=capability.source_format,
            target_format=capability.target_format,
            ext=ext,
            size_bytes=size_bytes,
            client_ip=_client_ip(request),
            auth_enforced=bool(config.API_KEYS),
        )
        start_time = time.monotonic()

        def _failed(reason: str) -> None:
            audit.log_event(
                "convert_failed",
                request_id=request_id,
                reason=reason,
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        try:
            _check_decompression_bomb(input_path, ext)
            future = _convert_executor.submit(
                capability.convert,
                input_path,
                output_path,
                progress=lambda msg: print(f"[{request_id}] {msg}"),
            )
            future.result(timeout=config.CONVERT_TIMEOUT_SECONDS)
        except security.FileTooLargeError as e:
            _failed("file_too_large")
            raise HTTPException(status_code=413, detail=str(e)) from e
        except FutureTimeoutError as e:
            _failed("timeout")
            raise HTTPException(
                status_code=504,
                detail=f"Conversion exceeded {config.CONVERT_TIMEOUT_SECONDS:.0f}s and was abandoned.",
            ) from e
        except EnvironmentError as e:
            # Tesseract missing/misconfigured -- a server problem, not a
            # bad request.
            _failed("environment_error")
            raise HTTPException(status_code=503, detail=str(e)) from e
        except Exception as e:
            # Per docs/PHASE_0_AUDIT.md failure philosophy: never expose a
            # raw stack trace to the caller. Log the real error server-side
            # (safe: this is a pipeline error, not document content) and
            # return a generic message.
            print(f"[{request_id}] conversion failed: {e!r}")
            _failed("internal_error")
            raise HTTPException(
                status_code=500,
                detail="Conversion failed. This has been logged for investigation.",
            ) from e

        audit.log_event(
            "convert_completed",
            request_id=request_id,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )
        result_bytes = output_path.read_bytes()
    finally:
        storage.release(work_dir)

    return Response(
        content=result_bytes,
        media_type=capability.media_type,
        headers={
            "Content-Disposition": f"attachment; filename=converted{capability.output_extension}"
        },
    )


def _run_job(
    job: Job, input_path: Path, output_path: Path, ext: str, capability: Capability, request_id: str
) -> None:
    """Runs on the shared executor, in the background -- the HTTP request
    that created this job has already returned by the time this runs."""
    _job_store.update(job.id, status="processing")
    start_time = time.monotonic()

    def _failed(reason: str) -> None:
        audit.log_event(
            "job_failed",
            request_id=request_id,
            job_id=job.id,
            reason=reason,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )

    try:
        _check_decompression_bomb(input_path, ext)
        capability.convert(
            input_path,
            output_path,
            progress=lambda msg: print(f"[{request_id}] {msg}"),
        )
        # A sibling file next to output_path, not something every
        # capability announces explicitly -- see converters/
        # ocr_to_excel.py's own note on why. Only OCR->Excel jobs
        # produce one today; every other capability's jobs get
        # review_path=None, and the review endpoints below 404 for those.
        review_path = output_path.parent / f"{output_path.stem}.review.json"
        _job_store.update(
            job.id,
            status="completed",
            result_path=output_path,
            review_path=review_path if review_path.exists() else None,
        )
        audit.log_event(
            "job_completed",
            request_id=request_id,
            job_id=job.id,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )
    except security.FileTooLargeError as e:
        _job_store.update(job.id, status="failed", error=str(e))
        _failed("file_too_large")
    except EnvironmentError as e:
        _job_store.update(job.id, status="failed", error=str(e))
        _failed("environment_error")
    except Exception as e:
        # Same failure philosophy as the sync endpoint: log the real error
        # server-side, expose only a generic message via the status endpoint.
        print(f"[{request_id}] job {job.id} failed: {e!r}")
        _job_store.update(
            job.id, status="failed", error="Conversion failed. This has been logged for investigation."
        )
        _failed("internal_error")


@app.post("/api/v1/jobs", dependencies=[Depends(require_api_key)], status_code=202)
def create_job(request: Request, file: UploadFile, target: str = Form("xlsx")) -> dict:
    """
    Accepts one file, validates it synchronously (so bad input is rejected
    immediately, not discovered later by polling), then queues the actual
    conversion in the background and returns right away with a job id to
    poll. `target` selects the capability, same as /api/v1/convert -- see
    that endpoint's docstring and GET /api/v1/capabilities.
    """
    _check_rate_limit(request)

    job = _job_store.create()
    work_dir = storage.allocate(f"docconv-job-{job.id}-")
    job.work_dir = work_dir

    try:
        input_path, ext = _validate_and_save_upload(request, file, work_dir)
        capability = _resolve_capability(ext, target)
        _check_decompression_bomb(input_path, ext)
    except HTTPException:
        storage.release(work_dir)
        _job_store.update(job.id, status="failed")
        raise
    except security.FileTooLargeError as e:
        storage.release(work_dir)
        raise HTTPException(status_code=413, detail=str(e)) from e

    output_path = work_dir / f"output{capability.output_extension}"
    _job_store.update(
        job.id,
        result_media_type=capability.media_type,
        result_filename=f"converted{capability.output_extension}",
    )
    request_id = uuid.uuid4().hex[:12]
    size_bytes = input_path.stat().st_size
    print(
        f"[{request_id}] job {job.id} queued: ext={ext} target={target} "
        f"size={size_bytes}B"
    )
    audit.log_event(
        "job_created",
        request_id=request_id,
        job_id=job.id,
        endpoint="async",
        source_format=capability.source_format,
        target_format=capability.target_format,
        ext=ext,
        size_bytes=size_bytes,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )

    _convert_executor.submit(_run_job, job, input_path, output_path, ext, capability, request_id)

    return {"job_id": job.id, "status": job.status}


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job_status(job_id: str) -> dict:
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    # has_review (Phase 7 completion, master directive numbering) lets a
    # caller know whether GET/POST .../review are worth calling at all,
    # without a separate probe request that would 404 for most jobs
    # (only OCR->Excel produces review data).
    body = {"job_id": job.id, "status": job.status, "has_review": job.review_path is not None}
    if job.error:
        body["error"] = job.error
    return body


@app.get("/api/v1/jobs/{job_id}/result", dependencies=[Depends(require_api_key)])
def get_job_result(job_id: str) -> Response:
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "completed":
        raise HTTPException(
            status_code=409, detail=f"Job is '{job.status}', not completed yet."
        )
    return Response(
        content=job.result_path.read_bytes(),
        media_type=job.result_media_type,
        headers={"Content-Disposition": f"attachment; filename={job.result_filename}"},
    )


@app.get("/api/v1/jobs/{job_id}/review", dependencies=[Depends(require_api_key)])
def get_job_review(job_id: str) -> dict:
    """
    Master directive Phase 7 completion: "preview" and "human review".
    Returns the structured table data a completed OCR->Excel job
    produced (documents_converter/converters/ocr_to_excel.py's
    review_json_path), one entry per detected table with every cell's
    value and suspicious flag -- lets a caller show, and let a person
    correct, the extracted data before trusting or downloading the final
    file. 404 for a job with no review data (any non-OCR->Excel target,
    or a job that hasn't reached "completed") rather than an empty or
    misleading response.
    """
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.review_path is None:
        raise HTTPException(
            status_code=404,
            detail="No review data for this job (only available for OCR-to-Excel conversions).",
        )
    return json.loads(job.review_path.read_text(encoding="utf-8"))


@app.post("/api/v1/jobs/{job_id}/review", dependencies=[Depends(require_api_key)])
def submit_job_review(job_id: str, corrections: list[dict]) -> dict:
    """
    Applies human corrections to a completed OCR->Excel job's cells --
    to both the stored review JSON (so a later GET reflects them) and
    the actual .xlsx result (so a later download does too). A corrected
    cell whose new value no longer looks suspicious (re-checked with the
    same documents_converter.ocr_excel._is_suspicious this project's
    Excel output itself uses) has its highlight cleared -- a person who
    fixed a flagged cell shouldn't see it still marked as uncertain.

    Each correction: {"sheet_name": str, "row_index": int,
    "col_index": int, "value": str} -- the same addressing the GET above
    returns cells with, which is also exactly what xlsxwriter wrote the
    .xlsx cell at (documents_converter/ocr_excel.py's
    _write_table_flagged).
    """
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.review_path is None:
        raise HTTPException(status_code=404, detail="No review data for this job.")

    review = json.loads(job.review_path.read_text(encoding="utf-8"))
    tables_by_name = {t["sheet_name"]: t for t in review["tables"]}
    workbook = openpyxl.load_workbook(job.result_path)

    applied = 0
    for correction in corrections:
        sheet_name = correction.get("sheet_name")
        row_index = correction.get("row_index")
        col_index = correction.get("col_index")
        value = correction.get("value")

        table = tables_by_name.get(sheet_name)
        if table is None or sheet_name not in workbook.sheetnames:
            raise HTTPException(status_code=400, detail=f"Unknown sheet '{sheet_name}'.")
        row_entry = next((r for r in table["rows"] if r["row_index"] == row_index), None)
        if row_entry is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown row {row_index} in '{sheet_name}'."
            )
        cell_entry = next((c for c in row_entry["cells"] if c["col_index"] == col_index), None)
        if cell_entry is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown cell (row {row_index}, col {col_index}) in '{sheet_name}'.",
            )

        cell_entry["value"] = value
        cell_entry["suspicious"] = _is_suspicious(value)

        # openpyxl is 1-indexed; row_index/col_index are the same
        # 0-indexed coordinates xlsxwriter originally wrote at.
        xlsx_cell = workbook[sheet_name].cell(row=row_index + 1, column=col_index + 1)
        xlsx_cell.value = value
        if not cell_entry["suspicious"]:
            xlsx_cell.fill = PatternFill(fill_type=None)
        applied += 1

    workbook.save(job.result_path)
    job.review_path.write_text(json.dumps(review), encoding="utf-8")

    audit.log_event("review_corrections_applied", job_id=job.id, count=applied)
    return {"applied": applied}


@app.exception_handler(Exception)
def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Catches anything not already turned into an HTTPException above (a
    # bug in this file, a Starlette-level error, etc.) so a raw traceback
    # can never reach the caller regardless of debug settings.
    print(f"Unhandled exception on {request.url.path}: {exc!r}")
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})
