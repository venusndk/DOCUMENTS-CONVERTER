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

Phase 11 (master directive numbering -- not this project's own,
differently-numbered earlier Phase 11, production-readiness hardening;
see README's disambiguation note) added POST /api/v1/pdf/*: eleven PDF
manipulation operations (pdf_utilities.py) -- merge, split, extract,
reorder, delete-pages, rotate, watermark, add-page-numbers, crop,
compress, repair. Deliberately its own family of endpoints, not routed
through the Capability registry/`target` field: merge needs multiple
input files, and most of the others need an operation-specific
parameter (a rotation angle, a page spec, watermark text) a single
`target` string has no room for. Synchronous only, like /convert -- no
job-queue variant, since every operation here is a fast, local,
in-process PyMuPDF call with nothing for that machinery to buy.

Phase 13 (master directive numbering) added POST /api/v1/batch and GET
/api/v1/batch/{id}[/download, /resume] (batch.py): multi-file upload
under one shared `target`, each file an ordinary job on the same
JobStore/job_queue/rq_tasks pipeline POST /api/v1/jobs already uses --
batch.py's BatchRecord is deliberately just the list of job ids
submitted together, not a parallel job-execution system. "Safe
concurrency" means exactly that reuse: a batch's files compete for the
same worker capacity every other conversion already shares (originally
a 4-thread in-process pool; Phase 14 below replaced that transport with
Redis/RQ without changing this principle), so one large batch can't
starve the rest of the service. A file that fails validation is
recorded as its own failed job immediately, without blocking the rest
of the batch from being queued -- individual failure reporting, both
from the status endpoint's per-file manifest and bundled into the
downloaded ZIP's manifest.json.

Phase 14 (master directive numbering) replaced that in-process
ThreadPoolExecutor with a real, separate job queue: Redis + RQ
(job_queue.py, rq_tasks.py), with actual worker processes
(worker_main.py; docker-compose.yml's `worker` service) pulling jobs
off it instead of the API process running conversions on its own
threads. Real job lifecycle (queued/processing/completed/failed/
cancelled), progress events (GET /api/v1/jobs/{id}/events, Server-Sent
Events reading the RQ job's own `meta`), automatic retry for
transient/environment failures (rq_tasks.run_conversion_job, never for
a bad-input failure that would just fail identically again), manual
retry/resumability (POST /api/v1/jobs/{id}/retry, POST
/api/v1/batch/{id}/resume -- re-enqueues a stuck/failed/cancelled job
reusing its already-saved input file, no re-upload needed), and
cancellation (POST /api/v1/jobs/{id}/cancel, RQ's own stop-job
mechanism for a job already in flight). The one non-obvious
consequence of a separate worker process (in Compose, a separate
container) is that it needs to see the exact same files the API
process wrote -- config.WORK_DIR_ROOT points both at a shared volume
instead of each container's own /tmp; see that config value's and
storage.py's docstrings.

Phase 12 (master directive numbering) added POST /api/v1/pdf/protect,
/unlock, /redact, /sign, and /verify-signatures (pdf_security.py):
password protection, encryption, and permissions (one PyMuPDF feature),
real content-removing redaction, and real cryptographic PDF signing via
pyHanko -- an ephemeral, self-signed demo certificate by default
(genuine tamper-evidence, no identity trust -- see
pdf_security.generate_demo_signer's docstring) or a caller-supplied
PKCS#12 certificate for identity-bound signing. /verify-signatures isn't
one of the master directive's five named sub-items for this phase, but
a signing endpoint with no way to check a signature would be a
half-built feature; added as a natural, disclosed complement to it, the
same way Phase 7's review-correction endpoints followed naturally from
its preview endpoint. Same family conventions as Phase 11: synchronous
only, .pdf-only input, its own request/response helpers reusing
_run_pdf_operation's audit logging and error mapping.

Phase 16 (master directive numbering) added POST /api/v1/ai/*:
vision-extract, extract, summarize, translate, classify
(documents_converter/ai_intelligence.py) -- a real semantic read of a
document via Google Gemini, not just structural inspection (that's
still document_analysis.py's job). Explicitly optional/configurable:
every endpoint here checks ai_intelligence.is_ai_available() first and
returns a clear 503 when GEMINI_API_KEY isn't set, the same shape as
the job queue's own 503 when Redis is unreachable. vision-extract,
extract, and classify accept a PDF (with `page`) or a plain image,
reusing /api/v1/preview's own upload validation and
document_preview.render_page_image normalization rather than
duplicating it. A genuine upstream failure (the provider's own error,
not this service's) maps to 502, distinct from this service's own 500.
Google Gemini, not Anthropic/Claude, for a concrete, disclosed reason
-- see ai_intelligence.py's and config.py's own docstrings.

Phase 17 (master directive numbering) added POST /api/v1/account/signup,
/login, /logout, GET/PUT /api/v1/account/preferences, GET
/api/v1/account/usage, GET /api/v1/account/history, and POST
/api/v1/jobs/{id}/save[/unsave] (documents_converter/api/accounts.py) --
real accounts, separate from, and additive to, auth.py's existing
pre-shared-key access control (see accounts.py's own docstring for
exactly how those two are different concerns). Every account endpoint
requires a valid session cookie (accounts.get_current_user, 401
otherwise) and, for anything mutating, a matching X-CSRF-Token header
(403 otherwise) -- POST /api/v1/jobs and /api/v1/batch instead use
accounts.get_current_user_optional to *optionally* tag a new job/batch
with whoever is logged in, never requiring it, so an unauthenticated or
API-key-only caller keeps working exactly as before.

Phase 18 (master directive numbering) added GET /admin (a real
dashboard page), GET /metrics (Prometheus text exposition format,
scraped continuously -- prometheus_client, updated by this module's own
_metrics_middleware plus admin.py's runtime Gauges), and GET
/api/v1/admin/queue, /providers, /analytics, /audit-log
(documents_converter/api/admin.py) -- not a new, separate admin-auth
mechanism, an extension of Phase 17's real accounts
(accounts.get_current_admin: an ordinary valid session, plus
User.is_admin, itself synced from config.ADMIN_EMAILS at signup/login).
GET /health stayed a fast, minimal liveness probe (Tesseract + the
database only) rather than growing every provider check Phase 18 added
-- GET /api/v1/admin/providers is the fuller picture, for a dashboard,
not a load balancer. audit.py (Phase 11) also gained a third
destination for every event it logs: a real database row
(AuditLogRecord), so GET /api/v1/admin/audit-log can show this
project's own audit trail back through the API instead of that history
only ever living in stdout/an optional file.

Run locally:
    uvicorn documents_converter.api.app:app --reload
"""

from __future__ import annotations

import base64
import json
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import openpyxl
import redis
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from openpyxl.styles import PatternFill
from PIL import Image as PILImage
from rq import Retry
from rq.command import send_stop_job_command
from rq.exceptions import NoSuchJobError
from rq.job import Job as RQJob

import fitz

from . import accounts, admin, audit, config, job_queue, security
from .auth import require_api_key
from .batch import Batch, BatchStore
from .db import check_db_connection
from .db import run_migrations as _run_migrations
from .jobs import Job, JobStore
from .rate_limit import FixedWindowRateLimiter
from .rq_tasks import run_conversion_job
from .storage import storage
from .. import converters  # noqa: F401 -- import for its registration side effect only
from .. import pdf_security, pdf_utilities
from .. import ai_intelligence, document_preview
from ..document_analysis import analyze
from ..ocr_excel import _is_suspicious, check_tesseract_available
from ..registry import Capability, registry


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
    # Phase 17 (master directive numbering): unlike API_KEYS, an
    # ephemeral SESSION_SECRET doesn't leave anything open-access -- a
    # deployment that never uses accounts at all isn't affected -- so
    # this is a warning, not a startup-refusing RuntimeError. See
    # config.SESSION_SECRET's own docstring for exactly what breaks
    # (every logged-in browser's CSRF token, not the session itself)
    # when this process restarts without one explicitly set.
    if config.ENVIRONMENT == "production" and not config.SESSION_SECRET_WAS_SET:
        print(
            "[startup] WARNING: SESSION_SECRET is not set -- using a random one generated "
            "for this process only. Every already-logged-in browser will need to log in "
            "again the next time this process restarts. Set SESSION_SECRET explicitly "
            "before relying on accounts surviving a restart/redeploy."
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _run_migrations()
    _check_startup_config()
    yield


app = FastAPI(title="Documents Converter API", version="0.1.0", lifespan=_lifespan)


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    """
    Phase 18 (master directive numbering): updates
    admin.HTTP_REQUESTS_TOTAL/HTTP_REQUEST_DURATION_SECONDS on every
    request -- the real, continuously-updated half of "metrics" (GET
    /metrics), distinct from admin.job_analytics()'s on-demand
    snapshot. Reads the matched route's own path *template* off
    `request.scope["route"]` after routing has happened (call_next
    already ran it), not `request.url.path` -- a raw path would put a
    fresh, unbounded label value in Prometheus for every distinct job
    id ever requested (GET /api/v1/jobs/{job_id} et al.), a real,
    well-known cardinality blowup this avoids by construction.
    """
    start = time.monotonic()
    response = await call_next(request)
    route = request.scope.get("route")
    path_label = getattr(route, "path", request.url.path)
    admin.HTTP_REQUESTS_TOTAL.labels(
        method=request.method, path=path_label, status=response.status_code
    ).inc()
    admin.HTTP_REQUEST_DURATION_SECONDS.labels(method=request.method, path=path_label).observe(
        time.monotonic() - start
    )
    return response


_rate_limiter = FixedWindowRateLimiter(
    max_requests=config.RATE_LIMIT_MAX_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
)
_job_store = JobStore(retention_seconds=config.JOB_RETENTION_SECONDS)
_batch_store = BatchStore(retention_seconds=config.JOB_RETENTION_SECONDS)
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


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_page() -> str:
    """
    Phase 18 (master directive numbering): the admin dashboard page.
    Served to anyone who requests it -- exactly like GET / -- because
    the real access control lives server-side, per request, on the API
    calls this page's own JS makes (accounts.get_current_admin: 401 if
    not logged in at all, 403 if logged in but not an admin), the same
    principle GET /api/v1/jobs/{id} already relies on rather than
    trying to keep a URL secret. A non-admin visiting this page sees
    exactly that -- a clear "admin access required" message, not a
    blank or broken page.
    """
    return (_STATIC_DIR / "admin.html").read_text(encoding="utf-8")


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


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """
    Phase 18 (master directive numbering): real Prometheus text
    exposition format (admin.render_metrics -- prometheus_client's own
    generate_latest), not a custom JSON shape -- plugs directly into
    real monitoring tooling (Prometheus, Grafana) with zero glue code.
    Unauthenticated, like GET /health and for the same stated reason
    (module docstring, Phase 6): a monitoring scraper needs to reach
    this without credentials, and network-level access control is the
    conventional way this endpoint is protected in real deployments,
    not an API key a scrape config would need to carry.
    """
    body, content_type = admin.render_metrics()
    return Response(content=body, media_type=content_type)


# --------------------------------------------------------------------------
# Phase 18 (master directive numbering): Administration & Observability
# --------------------------------------------------------------------------


@app.get("/api/v1/admin/queue", dependencies=[Depends(require_api_key)])
def admin_queue_status(_admin: accounts.Account = Depends(accounts.get_current_admin)) -> dict:
    return admin.queue_status()


@app.get("/api/v1/admin/providers", dependencies=[Depends(require_api_key)])
def admin_provider_status(_admin: accounts.Account = Depends(accounts.get_current_admin)) -> dict:
    return admin.provider_status()


@app.get("/api/v1/admin/analytics", dependencies=[Depends(require_api_key)])
def admin_job_analytics(_admin: accounts.Account = Depends(accounts.get_current_admin)) -> dict:
    return admin.job_analytics()


@app.get("/api/v1/admin/audit-log", dependencies=[Depends(require_api_key)])
def admin_audit_log(
    limit: int = 100,
    event: str | None = None,
    _admin: accounts.Account = Depends(accounts.get_current_admin),
) -> dict:
    return {"events": admin.query_audit_log(limit=limit, event=event)}


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


# Phase 14 (master directive numbering) moved the extension set and the
# actual check into security.py -- rq_tasks.py's worker-executed task
# needs the exact same check and must not import this module (a worker
# process has no reason to construct the FastAPI app). Kept as
# module-level names here since ~20 call sites below already use them.
_IMAGE_BOMB_CHECK_EXTENSIONS = security.IMAGE_BOMB_CHECK_EXTENSIONS
_check_decompression_bomb = security.check_decompression_bomb


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
        # document_analysis.analyze() only actually knows how to handle
        # PDF and traditional images -- checked against that directly,
        # not security.MAGIC_SIGNATURES (which is broader as of Phase 9's
        # new Office/HTML/Markdown capabilities; analysis hasn't been
        # extended to those formats and shouldn't silently pretend to).
        if ext != ".pdf" and ext not in _IMAGE_BOMB_CHECK_EXTENSIONS:
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


@app.post("/api/v1/preview", dependencies=[Depends(require_api_key)])
def preview_document(request: Request, file: UploadFile, page: int = Form(1)) -> dict:
    """
    Phase 15 (master directive numbering): a real, job-independent look
    at one page of an uploaded document -- its rendered image plus a
    bounded text preview (native text layer if present, real Tesseract
    OCR otherwise) -- before committing to a full /convert or /jobs
    request. Covers PDF preview, OCR preview, and extracted text preview
    together (documents_converter/document_preview.py); table preview
    is served by GET /api/v1/jobs/{id}/review instead, once a real job
    has actually run real table detection -- running that twice, once
    here and again for the real job, would waste exactly the compute
    the async job/queue pipeline exists to spare a caller from paying
    synchronously.
    """
    _check_rate_limit(request)

    work_dir = storage.allocate("docconv-preview-")
    try:
        input_path, ext = _validate_and_save_upload(request, file, work_dir)
        if ext != ".pdf" and ext not in _IMAGE_BOMB_CHECK_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot preview '{ext}' -- not a recognized PDF/image extension.",
            )

        request_id = uuid.uuid4().hex[:12]
        audit.log_event(
            "preview_requested",
            request_id=request_id,
            ext=ext,
            page=page,
            size_bytes=input_path.stat().st_size,
            client_ip=_client_ip(request),
            auth_enforced=bool(config.API_KEYS),
        )

        try:
            _check_decompression_bomb(input_path, ext)
            image_bytes = document_preview.render_page_image(input_path, ext, page)
            text, is_ocr, page_count = document_preview.extract_text_preview(input_path, ext, page)
        except ValueError as e:
            audit.log_event("preview_failed", request_id=request_id, reason="invalid_input")
            raise HTTPException(status_code=400, detail=str(e)) from e
        except security.FileTooLargeError as e:
            audit.log_event("preview_failed", request_id=request_id, reason="file_too_large")
            raise HTTPException(status_code=413, detail=str(e)) from e
        except Exception as e:
            print(f"[{request_id}] preview failed: {e!r}")
            audit.log_event("preview_failed", request_id=request_id, reason="internal_error")
            raise HTTPException(
                status_code=500, detail="Preview failed. This has been logged for investigation."
            ) from e

        audit.log_event(
            "preview_completed", request_id=request_id, page=page, page_count=page_count, is_ocr=is_ocr
        )
        return {
            "page": page,
            "page_count": page_count,
            "is_ocr": is_ocr,
            "text_preview": text,
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
        }
    finally:
        storage.release(work_dir)


# --------------------------------------------------------------------------
# Phase 16 (master directive numbering): AI Document Intelligence
# --------------------------------------------------------------------------


def _check_ai_available() -> None:
    if not ai_intelligence.is_ai_available():
        raise HTTPException(
            status_code=503,
            detail="AI features are not configured on this server (GEMINI_API_KEY is unset).",
        )


def _check_ai_text_length(text: str) -> None:
    if len(text) > config.AI_MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Text exceeds the {config.AI_MAX_TEXT_CHARS}-character limit for AI features.",
        )


def _read_ai_image(request: Request, file: UploadFile, page: int, *, request_id: str, event_prefix: str) -> bytes:
    """
    Shared by every /api/v1/ai/* endpoint that accepts an image: same
    upload validation as /api/v1/preview, then always normalized to PNG
    via document_preview.render_page_image -- a caller can hand this
    either a PDF (with `page`) or a plain image (page must be 1), same
    as that endpoint already does.
    """
    work_dir = storage.allocate("docconv-ai-")
    try:
        input_path, ext = _validate_and_save_upload(request, file, work_dir)
        if ext != ".pdf" and ext not in _IMAGE_BOMB_CHECK_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot read '{ext}' -- not a recognized PDF/image extension.",
            )
        try:
            _check_decompression_bomb(input_path, ext)
            return document_preview.render_page_image(input_path, ext, page)
        except ValueError as e:
            audit.log_event(f"{event_prefix}_failed", request_id=request_id, reason="invalid_input")
            raise HTTPException(status_code=400, detail=str(e)) from e
        except security.FileTooLargeError as e:
            audit.log_event(f"{event_prefix}_failed", request_id=request_id, reason="file_too_large")
            raise HTTPException(status_code=413, detail=str(e)) from e
        except Exception as e:
            print(f"[{request_id}] {event_prefix} image read failed: {e!r}")
            audit.log_event(f"{event_prefix}_failed", request_id=request_id, reason="internal_error")
            raise HTTPException(
                status_code=500,
                detail="Reading the uploaded document failed. This has been logged for investigation.",
            ) from e
    finally:
        storage.release(work_dir)


def _call_ai(request_id: str, event_prefix: str, fn: Callable[[], dict | str]):
    """
    Shared call-and-map-errors wrapper for the real Gemini request each
    endpoint below makes. AIUnavailableError (a config race between
    _check_ai_available() and this call) maps to 503, same as an
    upfront unavailable check. Anything else here is a genuine failure
    from the provider itself (rate limit, network error, malformed
    response) -- this service is working correctly, so 502 (upstream
    failure), not 500, and never a raw stack trace to the caller.
    """
    try:
        return fn()
    except ai_intelligence.AIUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        print(f"[{request_id}] {event_prefix} failed: {e!r}")
        audit.log_event(f"{event_prefix}_failed", request_id=request_id, reason="ai_provider_error")
        raise HTTPException(
            status_code=502,
            detail="The AI provider request failed. This has been logged for investigation.",
        ) from e


@app.post("/api/v1/ai/vision-extract", dependencies=[Depends(require_api_key)])
def ai_vision_extract(request: Request, file: UploadFile, page: int = Form(1)) -> dict:
    """
    Vision fallback: reads a page directly with a vision-capable model
    (ai_intelligence.vision_extract) for text Tesseract's own OCR reads
    poorly (unusual fonts, handwriting, low-contrast scans) -- not a
    replacement for the normal, faster, free /convert or /jobs OCR
    path. Accepts a PDF (with `page`) or a plain image, same
    upload/normalization as /api/v1/preview. Returns plain text, not
    structured data -- see /api/v1/ai/extract for that, given a real
    field list.
    """
    _check_ai_available()
    _check_rate_limit(request)

    request_id = uuid.uuid4().hex[:12]
    image_bytes = _read_ai_image(
        request, file, page, request_id=request_id, event_prefix="ai_vision_extract"
    )
    audit.log_event(
        "ai_vision_extract_requested",
        request_id=request_id,
        page=page,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )
    text = _call_ai(
        request_id, "ai_vision_extract", lambda: ai_intelligence.vision_extract(image_bytes)
    )
    audit.log_event("ai_vision_extract_completed", request_id=request_id, text_length=len(text))
    return {"text": text}


@app.post("/api/v1/ai/extract", dependencies=[Depends(require_api_key)])
def ai_extract_structured(
    request: Request,
    fields: str = Form(...),
    text: str | None = Form(None),
    file: UploadFile | None = File(None),
    page: int = Form(1),
) -> dict:
    """
    Structured extraction: given plain text or an image/PDF page and a
    comma-separated list of field names, returns a flat {field: value}
    JSON object (ai_intelligence.extract_structured) -- `null` for a
    field genuinely not present rather than a guessed value. Exactly
    one of `text`/`file` must be given.
    """
    _check_ai_available()
    _check_rate_limit(request)

    if (text is None) == (file is None):
        raise HTTPException(status_code=400, detail="Provide exactly one of `text` or `file`.")

    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    if not field_list:
        raise HTTPException(status_code=400, detail="`fields` must name at least one field.")

    request_id = uuid.uuid4().hex[:12]
    image_bytes = None
    if file is not None:
        image_bytes = _read_ai_image(
            request, file, page, request_id=request_id, event_prefix="ai_extract"
        )
    else:
        _check_ai_text_length(text)

    audit.log_event(
        "ai_extract_requested",
        request_id=request_id,
        field_count=len(field_list),
        source="image" if image_bytes is not None else "text",
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )
    result = _call_ai(
        request_id,
        "ai_extract",
        lambda: ai_intelligence.extract_structured(
            text=text, image_bytes=image_bytes, fields=field_list
        ),
    )
    audit.log_event("ai_extract_completed", request_id=request_id, field_count=len(field_list))
    return result


@app.post("/api/v1/ai/summarize", dependencies=[Depends(require_api_key)])
def ai_summarize(request: Request, text: str = Form(...), max_sentences: int = Form(5)) -> dict:
    """A plain-language summary of `text`, bounded to at most
    `max_sentences` sentences (ai_intelligence.summarize)."""
    _check_ai_available()
    _check_rate_limit(request)
    _check_ai_text_length(text)

    request_id = uuid.uuid4().hex[:12]
    audit.log_event(
        "ai_summarize_requested",
        request_id=request_id,
        text_length=len(text),
        max_sentences=max_sentences,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )
    summary = _call_ai(
        request_id, "ai_summarize", lambda: ai_intelligence.summarize(text, max_sentences)
    )
    audit.log_event("ai_summarize_completed", request_id=request_id, summary_length=len(summary))
    return {"summary": summary}


@app.post("/api/v1/ai/translate", dependencies=[Depends(require_api_key)])
def ai_translate(request: Request, text: str = Form(...), target_language: str = Form(...)) -> dict:
    """Translates `text` into `target_language` (ai_intelligence.translate)."""
    _check_ai_available()
    _check_rate_limit(request)
    _check_ai_text_length(text)

    request_id = uuid.uuid4().hex[:12]
    audit.log_event(
        "ai_translate_requested",
        request_id=request_id,
        text_length=len(text),
        target_language=target_language,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )
    translation = _call_ai(
        request_id, "ai_translate", lambda: ai_intelligence.translate(text, target_language)
    )
    audit.log_event(
        "ai_translate_completed", request_id=request_id, translation_length=len(translation)
    )
    return {"translation": translation}


@app.post("/api/v1/ai/classify", dependencies=[Depends(require_api_key)])
def ai_classify(
    request: Request,
    text: str | None = Form(None),
    file: UploadFile | None = File(None),
    page: int = Form(1),
    categories: str | None = Form(None),
) -> dict:
    """
    Intelligent classification: what kind of document this is, a real
    semantic read (ai_intelligence.classify) -- unlike
    document_analysis.py's own digital/scanned/mixed classification,
    which never looks at what the document actually says. `categories`,
    when given (comma-separated), constrains the answer to exactly one
    of that list. Exactly one of `text`/`file` must be given.
    """
    _check_ai_available()
    _check_rate_limit(request)

    if (text is None) == (file is None):
        raise HTTPException(status_code=400, detail="Provide exactly one of `text` or `file`.")

    category_list = [c.strip() for c in categories.split(",") if c.strip()] if categories else None

    request_id = uuid.uuid4().hex[:12]
    image_bytes = None
    if file is not None:
        image_bytes = _read_ai_image(
            request, file, page, request_id=request_id, event_prefix="ai_classify"
        )
    else:
        _check_ai_text_length(text)

    audit.log_event(
        "ai_classify_requested",
        request_id=request_id,
        source="image" if image_bytes is not None else "text",
        category_count=len(category_list) if category_list else 0,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )
    result = _call_ai(
        request_id,
        "ai_classify",
        lambda: ai_intelligence.classify(
            text=text, image_bytes=image_bytes, categories=category_list
        ),
    )
    audit.log_event("ai_classify_completed", request_id=request_id, category=result.get("category"))
    return result


# --------------------------------------------------------------------------
# Phase 17 (master directive numbering): Authentication & User Workspace
# --------------------------------------------------------------------------


@app.post("/api/v1/account/signup", dependencies=[Depends(require_api_key)])
def account_signup(request: Request, response: Response, credentials: dict) -> dict:
    """
    Creates a new account and immediately logs it in (same session
    cookies POST .../login sets) -- one round trip for the common case
    rather than forcing a separate login call right after signing up.
    `credentials`: {"email": "...", "password": "..."}.
    """
    _check_rate_limit(request)
    email, password = credentials.get("email"), credentials.get("password")
    if not email or not password:
        raise HTTPException(status_code=400, detail="`email` and `password` are both required.")

    try:
        account = accounts.accounts.signup(email, password)
    except (accounts.InvalidEmailError, accounts.WeakPasswordError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except accounts.EmailAlreadyRegisteredError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    raw_token = accounts.accounts.create_session(account.id)
    accounts.set_session_cookies(response, raw_token)
    audit.log_event("account_signup", user_id=account.id, client_ip=_client_ip(request))
    return {"id": account.id, "email": account.email}


@app.post("/api/v1/account/login", dependencies=[Depends(require_api_key)])
def account_login(request: Request, response: Response, credentials: dict) -> dict:
    _check_rate_limit(request)
    email, password = credentials.get("email"), credentials.get("password")
    if not email or not password:
        raise HTTPException(status_code=400, detail="`email` and `password` are both required.")

    try:
        account = accounts.accounts.login(email, password)
    except accounts.InvalidEmailError:
        # Same 401 as a wrong password -- a malformed email shouldn't
        # confirm anything different to the caller either.
        raise HTTPException(status_code=401, detail="Incorrect email or password.") from None
    except accounts.InvalidCredentialsError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    raw_token = accounts.accounts.create_session(account.id)
    accounts.set_session_cookies(response, raw_token)
    audit.log_event("account_login", user_id=account.id, client_ip=_client_ip(request))
    return {"id": account.id, "email": account.email}


@app.post("/api/v1/account/logout", dependencies=[Depends(require_api_key)])
def account_logout(
    request: Request,
    response: Response,
    current_user: accounts.Account = Depends(accounts.get_current_user),
) -> dict:
    raw_token = request.cookies.get(accounts.SESSION_COOKIE_NAME)
    if raw_token:
        accounts.accounts.delete_session(raw_token)
    accounts.clear_session_cookies(response)
    audit.log_event("account_logout", user_id=current_user.id)
    return {"logged_out": True}


@app.get("/api/v1/account/me", dependencies=[Depends(require_api_key)])
def account_me(current_user: accounts.Account = Depends(accounts.get_current_user)) -> dict:
    return {
        "id": current_user.id,
        "email": current_user.email,
        "created_at": current_user.created_at,
        # Phase 18 (master directive numbering): lets the admin
        # dashboard (GET /admin) tell "not an admin" from "not logged
        # in at all" without needing to attempt an admin-only call
        # first just to read its status code.
        "is_admin": current_user.is_admin,
    }


@app.get("/api/v1/account/preferences", dependencies=[Depends(require_api_key)])
def get_account_preferences(current_user: accounts.Account = Depends(accounts.get_current_user)) -> dict:
    return current_user.preferences


@app.put("/api/v1/account/preferences", dependencies=[Depends(require_api_key)])
def put_account_preferences(
    preferences: dict, current_user: accounts.Account = Depends(accounts.get_current_user)
) -> dict:
    """Replaces the whole preferences object -- deliberately not a
    partial merge, so a caller always knows exactly what's stored after
    this call without needing to also GET it first."""
    return accounts.accounts.set_preferences(current_user.id, preferences)


@app.get("/api/v1/account/usage", dependencies=[Depends(require_api_key)])
def get_account_usage(current_user: accounts.Account = Depends(accounts.get_current_user)) -> dict:
    """Job counts only -- this project's core resource. Phase 16's AI
    calls aren't tracked per-account yet (their own audit trail is
    anonymous, predating accounts) -- a real, disclosed gap, not
    silently ignored, and a natural extension for a later phase, not
    this one."""
    return _job_store.usage_for_user(current_user.id)


@app.get("/api/v1/account/history", dependencies=[Depends(require_api_key)])
def get_account_history(current_user: accounts.Account = Depends(accounts.get_current_user)) -> dict:
    """A logged-in user's own past jobs, newest first -- both saved and
    unsaved (see JobRecord.saved's own docstring: that flag only ever
    affects retention, never visibility here)."""
    jobs = _job_store.list_for_user(current_user.id)
    return {
        "jobs": [
            {
                "job_id": job.id,
                "status": job.status,
                "created_at": job.created_at,
                "target": job.target,
                "saved": job.saved,
            }
            for job in jobs
        ]
    }


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


def _enqueue_conversion_job(job_id: str, input_path: Path, ext: str, target: str, request_id: str) -> None:
    """
    Submits one job to Redis/RQ (job_queue.py, rq_tasks.run_conversion_job)
    -- shared by create_job and create_batch, the only two places that
    ever enqueue a conversion. Phase 14 (master directive numbering)
    replaced this function's previous in-process
    ThreadPoolExecutor.submit(_run_job, ...) body with a real,
    separate-process job queue -- see job_queue.py and rq_tasks.py's own
    docstrings for the resulting constraints (only plain str/int
    arguments cross the queue; the Capability itself is re-resolved
    fresh inside the worker).

    `job_id=job_id` gives the RQ job the exact same id as this
    project's own JobRecord for it -- one id, not two, so
    /jobs/{id}/cancel and /jobs/{id}/events can look either up by the
    same value a caller already has. `retry=Retry(max=...)` is RQ's own
    automatic-retry mechanism; see rq_tasks.run_conversion_job's
    docstring for exactly which failures it applies to (never a bad
    input, since retrying that can't ever produce a different result).

    Raises HTTPException(503) if Redis itself is unreachable, rather
    than letting a raw connection error surface as a generic 500 --
    Redis is now a real external dependency the async conversion
    pipeline can't function without, the same way a missing Tesseract
    binary already gets its own clear error path elsewhere.
    """
    try:
        job_queue.get_queue().enqueue(
            run_conversion_job,
            job_id,
            str(input_path),
            ext,
            target,
            request_id,
            job_id=job_id,
            job_timeout=config.CONVERT_TIMEOUT_SECONDS,
            retry=Retry(max=config.JOB_MAX_RETRIES, interval=[5, 15]),
        )
    except redis.exceptions.RedisError as e:
        _job_store.update(job_id, status="failed", error="Job queue (Redis) is unavailable.")
        raise HTTPException(
            status_code=503, detail="Job queue is temporarily unavailable. Try again shortly."
        ) from e


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

    # Phase 17 (master directive numbering): tags this job with whoever
    # is logged in, if anyone -- see accounts.get_current_user_optional's
    # own docstring for why this never requires login or enforces CSRF.
    current_user = accounts.get_current_user_optional(request)
    job = _job_store.create(target=target, user_id=current_user.id if current_user else None)
    work_dir = storage.allocate(f"docconv-job-{job.id}-")
    job.work_dir = work_dir
    # Persisted immediately, not just set on this local `job` object --
    # see JobRecord.work_dir's own docstring plus _resume_one_job, both
    # Phase 14 additions (master directive numbering) that were the
    # first things to actually need this column populated. Previously
    # it silently stayed NULL forever (found the hard way: a real
    # /retry request against a job that had genuinely completed 410'd
    # as "unavailable", tracing back to this line never having run) --
    # which also meant _cleanup_expired's own `if record.work_dir:
    # storage.release(...)` never fired for a single job, ever, a
    # pre-existing resource leak this phase's own testing surfaced.
    _job_store.update(job.id, work_dir=work_dir)

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

    _enqueue_conversion_job(job.id, input_path, ext, target, request_id)

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


def _fetch_rq_job(job_id: str) -> RQJob | None:
    """RQ jobs share their id with this project's own JobRecord (see
    _enqueue_conversion_job's job_id=job_id). None for a job that was
    never enqueued (failed validation before queuing) or has aged out
    of Redis -- both normal, not error, conditions for every caller
    below."""
    try:
        return RQJob.fetch(job_id, connection=job_queue.get_redis_connection())
    except NoSuchJobError:
        return None


@app.post("/api/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_job(job_id: str) -> dict:
    """
    Cancels a job that hasn't reached a terminal status yet: a queued
    job is removed from the queue before any worker picks it up; a
    processing job's worker is asked to stop via RQ's own
    send_stop_job_command (best-effort -- if the worker has already
    moved past a point where it checks for that signal, or has crashed
    entirely, this project's own JobRecord status is still set to
    "cancelled" regardless, since that status is this API's actual
    source of truth for every other endpoint here, not RQ's). A job
    already completed/failed/cancelled returns 409, not a silent
    no-op -- a caller should never mistake "too late to cancel" for
    "cancelled".
    """
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Job is already '{job.status}'; nothing to cancel.")

    rq_job = _fetch_rq_job(job_id)
    if rq_job is not None:
        if job.status == "processing":
            try:
                send_stop_job_command(job_queue.get_redis_connection(), job_id)
            except Exception:
                pass
        else:
            try:
                rq_job.cancel()
            except Exception:
                pass
    _job_store.update(job_id, status="cancelled", error="Cancelled by request.")
    audit.log_event("job_cancelled", job_id=job_id)
    return {"job_id": job_id, "status": "cancelled"}


def _resume_one_job(job: Job) -> str:
    """
    Re-enqueues one non-completed job, reusing the same input file
    already sitting in its work_dir -- no re-upload needed. This is
    what "resumability" actually means for this service: shared by
    both POST /jobs/{id}/retry (one job) and POST /batch/{id}/resume
    (every non-completed job in a batch at once), and by both callers'
    own real-container tests, which kill a worker mid-conversion,
    confirm the job is left stuck "processing" with no live RQ job
    behind it, then call this to prove it actually resumes rather than
    staying stuck forever.

    Returns a short outcome string rather than raising, so a caller
    iterating many jobs (batch resume) can report a per-job outcome
    instead of one job's problem aborting the rest:
    "queued" (re-enqueued), "already_completed", "already_processing"
    (a live RQ job still owns it), "unavailable" (its work_dir/input
    file or `target` didn't survive -- e.g. past its retention window),
    "queue_unavailable" (Redis itself is unreachable right now).
    """
    if job.status == "completed":
        return "already_completed"
    if job.status == "processing":
        rq_job = _fetch_rq_job(job.id)
        # RQ keeps a finished/failed job's record around for a while
        # after it's done (result_ttl) -- fetching it successfully does
        # NOT mean a worker is still actively running it. Confirmed the
        # hard way: an earlier version of this check treated any
        # fetchable RQ job as "still owned by a live worker" and
        # refused to resume a job whose worker had already finished (or
        # crashed) long ago, exactly the stuck case this function
        # exists to fix.
        if rq_job is not None and rq_job.get_status() in ("queued", "started", "deferred", "scheduled"):
            return "already_processing"
    if job.target is None or job.work_dir is None or not job.work_dir.exists():
        return "unavailable"
    input_candidates = list(job.work_dir.glob("input.*"))
    if not input_candidates:
        return "unavailable"
    input_path = input_candidates[0]
    ext = input_path.suffix

    request_id = uuid.uuid4().hex[:12]
    _job_store.update(job.id, status="queued", error=None)
    audit.log_event("job_retry_requested", job_id=job.id, request_id=request_id)
    try:
        _enqueue_conversion_job(job.id, input_path, ext, job.target, request_id)
    except HTTPException:
        return "queue_unavailable"
    return "queued"


@app.post("/api/v1/jobs/{job_id}/retry", dependencies=[Depends(require_api_key)])
def retry_job(job_id: str) -> dict:
    """Manual retry/resume for one job -- see _resume_one_job. Distinct
    from RQ's own automatic Retry (rq_tasks.py): this is for a job a
    caller has given up waiting on (stuck, cancelled, or exhausted its
    automatic retries), not something the queue does by itself."""
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    outcome = _resume_one_job(job)
    if outcome == "already_completed":
        raise HTTPException(status_code=409, detail="Job already completed; nothing to retry.")
    if outcome == "already_processing":
        raise HTTPException(status_code=409, detail="Job is actively processing.")
    if outcome == "unavailable":
        raise HTTPException(
            status_code=410, detail="This job's input file is no longer available to retry."
        )
    if outcome == "queue_unavailable":
        raise HTTPException(status_code=503, detail="Job queue is temporarily unavailable.")
    return {"job_id": job_id, "status": "queued"}


def _set_job_saved(job_id: str, current_user: accounts.Account, saved: bool) -> dict:
    """Shared by both endpoints below -- see JobRecord.saved's own
    docstring (models.py) for why this is a per-job opt-in rather than
    "every logged-in user's job lives forever", and
    accounts.get_current_user_optional's docstring (used at job
    creation) for why an anonymous job (user_id is None) has no owner
    to check against here."""
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.user_id != current_user.id:
        # Same status for "belongs to someone else" and "was never tied
        # to any account" -- neither should confirm to the caller
        # whether a job id they don't own even exists.
        raise HTTPException(status_code=403, detail="This job doesn't belong to your account.")
    updated = _job_store.set_saved(job_id, saved)
    return {"job_id": job_id, "saved": updated.saved}


@app.post("/api/v1/jobs/{job_id}/save", dependencies=[Depends(require_api_key)])
def save_job(job_id: str, current_user: accounts.Account = Depends(accounts.get_current_user)) -> dict:
    """Phase 17 (master directive numbering): exempts this one job from
    JobStore's normal retention-window cleanup. Requires a logged-in
    session and ownership of the job (403 for someone else's, or an
    anonymous one)."""
    return _set_job_saved(job_id, current_user, True)


@app.post("/api/v1/jobs/{job_id}/unsave", dependencies=[Depends(require_api_key)])
def unsave_job(job_id: str, current_user: accounts.Account = Depends(accounts.get_current_user)) -> dict:
    """Reverses POST .../save -- the job goes back to the normal
    retention-window cleanup once it next qualifies (terminal status,
    past JOB_RETENTION_SECONDS)."""
    return _set_job_saved(job_id, current_user, False)


@app.get("/api/v1/jobs/{job_id}/events", dependencies=[Depends(require_api_key)])
def job_events(job_id: str) -> StreamingResponse:
    """
    Server-Sent Events: streams this job's status and progress message
    (rq_tasks.py's _report_progress, read from the RQ job's own `meta`)
    as they change, one `data:` line per change, ending the stream once
    the job reaches a terminal status. SSE, not WebSockets -- this is
    one-directional (server to caller only) and works over plain HTTP
    with no separate protocol upgrade, which is all "watch one job's
    progress in real time" actually needs.
    """

    def _stream():
        last_payload = None
        # A generous but finite cap -- an SSE connection is still a real
        # open connection holding a worker thread; this ends it well
        # past any conversion this service's own CONVERT_TIMEOUT_SECONDS
        # would already have given up on, rather than holding it open
        # forever for a job id that will never reach a terminal status.
        deadline = time.monotonic() + config.CONVERT_TIMEOUT_SECONDS + 60
        while time.monotonic() < deadline:
            job = _job_store.get(job_id)
            if job is None:
                yield f"event: error\ndata: {json.dumps({'error': 'Job not found.'})}\n\n"
                return
            rq_job = _fetch_rq_job(job_id)
            progress = rq_job.get_meta().get("progress") if rq_job is not None else None
            payload = {"status": job.status, "progress": progress}
            if payload != last_payload:
                yield f"data: {json.dumps(payload)}\n\n"
                last_payload = payload
            if job.status in ("completed", "failed", "cancelled"):
                return
            time.sleep(0.5)

    return StreamingResponse(_stream(), media_type="text/event-stream")


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


def _find_job_input_file(job: Job) -> Path:
    """Locates the original uploaded file still sitting in a job's
    work_dir -- same "input.*" convention _validate_and_save_upload
    always writes to, same glob Phase 14's _resume_one_job already
    relies on. Raises HTTPException(410) if the job's work_dir (or the
    file in it) didn't survive -- past its retention window, same as a
    job too old to retry."""
    if job.work_dir is None or not job.work_dir.exists():
        raise HTTPException(
            status_code=410, detail="This job's original file is no longer available."
        )
    candidates = list(job.work_dir.glob("input.*"))
    if not candidates:
        raise HTTPException(
            status_code=410, detail="This job's original file is no longer available."
        )
    return candidates[0]


@app.get("/api/v1/jobs/{job_id}/preview/pages/{page_number}", dependencies=[Depends(require_api_key)])
def get_job_page_preview(job_id: str, page_number: int) -> Response:
    """
    Phase 15 (master directive numbering): renders one 1-indexed page
    of the ORIGINAL file behind this job as a PNG -- the visual half of
    "table preview" and "OCR preview" that GET .../review's editable
    table data has never had on its own. Every review-JSON table's
    `sheet_name` already encodes its 1-indexed source page ("Page N -
    Table M"), so the frontend pairs this endpoint's image with the
    matching review table using that number directly, without any new
    field on the review JSON itself.
    """
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    input_path = _find_job_input_file(job)

    try:
        image_bytes = document_preview.render_page_image(input_path, input_path.suffix, page_number)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Response(content=image_bytes, media_type="image/png")


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


# --------------------------------------------------------------------------
# Phase 13 (master directive numbering): Batch Processing.
# --------------------------------------------------------------------------


def _batch_manifest(batch: Batch) -> list[dict]:
    """Per-file status for a batch -- job order preserved, exactly as
    submitted, and each entry only as much as its own job knows about
    itself. A referenced job that no longer exists (JobStore's own
    retention cleanup ran before this batch's) is reported as its own
    distinct status rather than silently skipped or mistaken for a job
    that is still queued."""
    entries = []
    for job_id in batch.job_ids:
        job = _job_store.get(job_id)
        if job is None:
            entries.append({"job_id": job_id, "status": "expired"})
            continue
        entry = {"job_id": job.id, "status": job.status}
        if job.error:
            entry["error"] = job.error
        entries.append(entry)
    return entries


def _batch_overall_status(manifest: list[dict]) -> str:
    statuses = {entry["status"] for entry in manifest}
    if statuses <= {"completed"}:
        return "completed"
    if statuses <= {"failed", "expired", "cancelled"}:
        return "failed"
    if statuses <= {"completed", "failed", "expired", "cancelled"}:
        return "completed_with_errors"
    if "processing" in statuses:
        return "processing"
    return "queued"


@app.post("/api/v1/batch", dependencies=[Depends(require_api_key)], status_code=202)
def create_batch(request: Request, files: list[UploadFile] = File(...), target: str = Form("xlsx")) -> dict:
    """
    Accepts multiple files under one `target`, queuing each as its own
    ordinary job on the exact same Redis/RQ pipeline POST /api/v1/jobs
    uses (_enqueue_conversion_job -- see batch.py's module docstring
    for why that's what "safe concurrency" means for a batch: every
    file competes for the same worker capacity every other conversion
    already shares, not a separate uncapped pool).

    Per-file failure reporting starts immediately, not just at the end:
    a file that fails validation (wrong extension for `target`, bad
    magic bytes, a decompression-bomb page/pixel count) is recorded as
    its own failed job right away and does NOT prevent the rest of the
    batch's files from being validated and queued -- one bad file in a
    batch of fifty should cost that one file, not the other forty-nine.
    Poll GET /api/v1/batch/{batch_id} for progress, GET .../download
    once every file has reached a terminal state.
    """
    _check_rate_limit(request)
    if not files:
        raise HTTPException(status_code=400, detail="Batch needs at least 1 file.")

    # Phase 17 (master directive numbering): see create_job's identical note.
    current_user = accounts.get_current_user_optional(request)
    owner_id = current_user.id if current_user else None

    job_ids = []
    for file in files:
        job = _job_store.create(target=target, user_id=owner_id)
        work_dir = storage.allocate(f"docconv-job-{job.id}-")
        job.work_dir = work_dir
        _job_store.update(job.id, work_dir=work_dir)  # see create_job's identical note
        job_ids.append(job.id)

        try:
            input_path, ext = _validate_and_save_upload(request, file, work_dir)
            capability = _resolve_capability(ext, target)
            _check_decompression_bomb(input_path, ext)
        except HTTPException as e:
            storage.release(work_dir)
            _job_store.update(job.id, status="failed", error=str(e.detail))
            continue
        except security.FileTooLargeError as e:
            storage.release(work_dir)
            _job_store.update(job.id, status="failed", error=str(e))
            continue

        _job_store.update(
            job.id,
            result_media_type=capability.media_type,
            result_filename=f"converted{capability.output_extension}",
        )
        request_id = uuid.uuid4().hex[:12]
        audit.log_event(
            "job_created",
            request_id=request_id,
            job_id=job.id,
            endpoint="batch",
            source_format=capability.source_format,
            target_format=capability.target_format,
            ext=ext,
            size_bytes=input_path.stat().st_size,
            client_ip=_client_ip(request),
            auth_enforced=bool(config.API_KEYS),
        )
        try:
            _enqueue_conversion_job(job.id, input_path, ext, target, request_id)
        except HTTPException:
            # Redis unreachable -- this one file's job is recorded failed
            # (inside _enqueue_conversion_job) but the rest of the batch's
            # files still deserve a shot at being queued, same "one bad
            # file costs that file, not the batch" principle as a
            # validation failure above.
            continue

    batch = _batch_store.create(target, job_ids, user_id=owner_id)
    audit.log_event(
        "batch_created",
        batch_id=batch.id,
        total=batch.total,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
    )
    return {"batch_id": batch.id, "total": batch.total, "job_ids": job_ids}


@app.get("/api/v1/batch/{batch_id}", dependencies=[Depends(require_api_key)])
def get_batch_status(batch_id: str) -> dict:
    """Aggregated progress: overall `status` (queued/processing/
    completed/completed_with_errors/failed -- see _batch_overall_status),
    per-file counts, and the full per-file manifest (_batch_manifest)."""
    batch = _batch_store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found.")

    manifest = _batch_manifest(batch)
    counts: dict[str, int] = {}
    for entry in manifest:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {
        "batch_id": batch.id,
        "status": _batch_overall_status(manifest),
        "total": batch.total,
        "counts": counts,
        "files": manifest,
    }


@app.get("/api/v1/batch/{batch_id}/download", dependencies=[Depends(require_api_key)])
def get_batch_download(batch_id: str) -> Response:
    """
    One ZIP: every successfully completed file's result (named by its
    job id, keeping each file's own extension), plus a manifest.json at
    the ZIP root with every file's status (including failures and their
    error messages) -- individual failure reporting travels with the
    results, not just a separate status-check call. 409 while any file
    is still queued/processing, the same "not ready yet" convention as
    GET /api/v1/jobs/{id}/result.
    """
    batch = _batch_store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found.")

    manifest = _batch_manifest(batch)
    overall = _batch_overall_status(manifest)
    if overall in ("queued", "processing"):
        raise HTTPException(status_code=409, detail=f"Batch is '{overall}', not finished yet.")

    work_dir = storage.allocate("docconv-batch-zip-")
    try:
        zip_path = work_dir / "batch-results.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for job_id in batch.job_ids:
                job = _job_store.get(job_id)
                if job is not None and job.status == "completed" and job.result_path:
                    zf.writestr(f"{job_id}{job.result_filename[job.result_filename.rfind('.'):]}", job.result_path.read_bytes())
            zf.writestr("manifest.json", json.dumps({"batch_id": batch.id, "files": manifest}, indent=2))
        data = zip_path.read_bytes()
    finally:
        storage.release(work_dir)

    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=batch-results.zip"},
    )


@app.post("/api/v1/batch/{batch_id}/resume", dependencies=[Depends(require_api_key)])
def resume_batch(batch_id: str) -> dict:
    """
    Resumability at the batch level: re-enqueues every file in the
    batch that isn't completed (see _resume_one_job) -- exercised for
    real against a live docker-compose stack: kill the worker container
    mid-batch, confirm the still-in-flight files are left "processing"
    with no live RQ job behind them, restart a worker, call this, and
    confirm those files complete without any re-upload. Per-file
    outcomes, not one pass/fail for the whole batch -- a file whose
    input no longer exists (its retention window expired) is reported
    as its own "unavailable" outcome, not an error that blocks
    resuming the rest.
    """
    batch = _batch_store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found.")

    outcomes = {}
    for job_id in batch.job_ids:
        job = _job_store.get(job_id)
        outcomes[job_id] = "expired" if job is None else _resume_one_job(job)
    audit.log_event("batch_resume_requested", batch_id=batch_id, outcomes=outcomes)
    return {"batch_id": batch_id, "outcomes": outcomes}


# --------------------------------------------------------------------------
# Phase 11 (master directive numbering): PDF Utilities.
# --------------------------------------------------------------------------


def _run_pdf_operation(operation: str, request_id: str, fn: Callable[[], object]) -> object:
    """
    Shared boilerplate every /api/v1/pdf/* endpoint needs around its
    actual pdf_utilities.* call: audit logging and error mapping.
    Returns whatever `fn` returns (split_pdf's written-file list, for
    instance); raises HTTPException on failure. Callers still need
    their own try/finally for storage cleanup around this, since that's
    needed whether this succeeds or fails.
    """
    start_time = time.monotonic()
    try:
        result = fn()
    except ValueError as e:
        audit.log_event(
            "pdf_utility_failed", request_id=request_id, operation=operation, reason="invalid_input"
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except security.FileTooLargeError as e:
        audit.log_event(
            "pdf_utility_failed", request_id=request_id, operation=operation, reason="file_too_large"
        )
        raise HTTPException(status_code=413, detail=str(e)) from e
    except Exception as e:
        print(f"[{request_id}] pdf utility '{operation}' failed: {e!r}")
        audit.log_event(
            "pdf_utility_failed", request_id=request_id, operation=operation, reason="internal_error"
        )
        raise HTTPException(
            status_code=500, detail="Operation failed. This has been logged for investigation."
        ) from e

    audit.log_event(
        "pdf_utility_completed",
        request_id=request_id,
        operation=operation,
        duration_ms=int((time.monotonic() - start_time) * 1000),
    )
    return result


def _pdf_response(path: Path, filename: str) -> Response:
    return Response(
        content=path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _require_pdf_upload(request: Request, file: UploadFile, work_dir: Path) -> tuple[Path, str]:
    """_validate_and_save_upload, plus the .pdf-only check every
    /api/v1/pdf/* endpoint needs -- unlike /convert and /jobs, there's
    no `target` to resolve a capability from here; every one of these
    operations only ever accepts PDF input."""
    input_path, ext = _validate_and_save_upload(request, file, work_dir)
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail=f"Expected a .pdf file, got '{ext}'.")
    return input_path, ext


def _new_pdf_request_id(request: Request, operation: str, **fields) -> str:
    request_id = uuid.uuid4().hex[:12]
    audit.log_event(
        "pdf_utility_requested",
        request_id=request_id,
        operation=operation,
        client_ip=_client_ip(request),
        auth_enforced=bool(config.API_KEYS),
        **fields,
    )
    return request_id


@app.post("/api/v1/pdf/merge", dependencies=[Depends(require_api_key)])
def pdf_merge(request: Request, files: list[UploadFile] = File(...)) -> Response:
    _check_rate_limit(request)
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Merge needs at least 2 files.")

    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_paths = []
        for i, f in enumerate(files):
            # _validate_and_save_upload always writes to a fixed
            # "input<ext>" name -- fine for every other endpoint here
            # (one file each), but multiple files in the same work_dir
            # would collide on that name, so each is renamed to its own
            # slot immediately after saving, before the next file's
            # save reuses "input.pdf".
            input_path, ext = _validate_and_save_upload(request, f, work_dir)
            if ext != ".pdf":
                raise HTTPException(status_code=400, detail=f"Expected a .pdf file, got '{ext}'.")
            renamed = work_dir / f"input-{i}.pdf"
            input_path.rename(renamed)
            _check_decompression_bomb(renamed, ".pdf")
            input_paths.append(renamed)

        request_id = _new_pdf_request_id(request, "merge", file_count=len(input_paths))
        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "merge", request_id, lambda: pdf_utilities.merge_pdfs(input_paths, output_path)
        )
        return _pdf_response(output_path, "merged.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/split", dependencies=[Depends(require_api_key)])
def pdf_split(request: Request, file: UploadFile, ranges: str | None = Form(None)) -> Response:
    """
    Splits into multiple PDFs, returned as one ZIP. `ranges`:
    semicolon-separated groups, each a comma/dash page spec (e.g.
    "1-2;3;4-5") -- one output file per group, in the given page order
    within each group. Omit for the default: one file per page.
    """
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "split")

        parsed_ranges = None
        if ranges:
            with fitz.open(str(input_path)) as doc:
                page_count = len(doc)
            try:
                parsed_ranges = [
                    pdf_utilities.parse_page_spec(part, page_count)
                    for part in ranges.split(";")
                    if part.strip()
                ]
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        out_dir = work_dir / "parts"
        out_dir.mkdir()
        parts = _run_pdf_operation(
            "split", request_id, lambda: pdf_utilities.split_pdf(input_path, out_dir, parsed_ranges)
        )

        zip_path = work_dir / "output.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for part_path in parts:
                zf.write(part_path, part_path.name)
        return Response(
            content=zip_path.read_bytes(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=split.zip"},
        )
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/extract", dependencies=[Depends(require_api_key)])
def pdf_extract(request: Request, file: UploadFile, pages: str = Form(...)) -> Response:
    """Writes one new PDF containing just `pages` (e.g. "1,3,5-7"), in
    the given order -- may reorder or repeat pages."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "extract")

        with fitz.open(str(input_path)) as doc:
            page_count = len(doc)
        try:
            page_list = pdf_utilities.parse_page_spec(pages, page_count)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "extract",
            request_id,
            lambda: pdf_utilities.extract_pages(input_path, output_path, page_list),
        )
        return _pdf_response(output_path, "extracted.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/reorder", dependencies=[Depends(require_api_key)])
def pdf_reorder(request: Request, file: UploadFile, order: str = Form(...)) -> Response:
    """Writes a new PDF with pages in the given order (e.g. "3,1,2") --
    `order` must name every page exactly once; use extract for a subset."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "reorder")

        with fitz.open(str(input_path)) as doc:
            page_count = len(doc)
        try:
            order_list = pdf_utilities.parse_page_spec(order, page_count)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "reorder",
            request_id,
            lambda: pdf_utilities.reorder_pages(input_path, output_path, order_list),
        )
        return _pdf_response(output_path, "reordered.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/delete-pages", dependencies=[Depends(require_api_key)])
def pdf_delete_pages(request: Request, file: UploadFile, pages: str = Form(...)) -> Response:
    """Writes a new PDF with `pages` (e.g. "2,4") removed."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "delete-pages")

        with fitz.open(str(input_path)) as doc:
            page_count = len(doc)
        try:
            page_list = pdf_utilities.parse_page_spec(pages, page_count)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "delete-pages",
            request_id,
            lambda: pdf_utilities.delete_pages(input_path, output_path, page_list),
        )
        return _pdf_response(output_path, "deleted.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/rotate", dependencies=[Depends(require_api_key)])
def pdf_rotate(
    request: Request, file: UploadFile, degrees: int = Form(...), pages: str | None = Form(None)
) -> Response:
    """Rotates `pages` (e.g. "1,3"; every page if omitted) clockwise by
    `degrees` (must be a multiple of 90)."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "rotate", degrees=degrees)

        page_list = None
        if pages:
            with fitz.open(str(input_path)) as doc:
                page_count = len(doc)
            try:
                page_list = pdf_utilities.parse_page_spec(pages, page_count)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "rotate",
            request_id,
            lambda: pdf_utilities.rotate_pages(input_path, output_path, degrees, page_list),
        )
        return _pdf_response(output_path, "rotated.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/watermark", dependencies=[Depends(require_api_key)])
def pdf_watermark(request: Request, file: UploadFile, text: str = Form(...)) -> Response:
    """Stamps `text` diagonally, semi-transparent, across every page."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "watermark")

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "watermark",
            request_id,
            lambda: pdf_utilities.add_watermark(input_path, output_path, text),
        )
        return _pdf_response(output_path, "watermarked.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/add-page-numbers", dependencies=[Depends(require_api_key)])
def pdf_add_page_numbers(request: Request, file: UploadFile, start: int = Form(1)) -> Response:
    """Stamps a page number at the bottom-center of every page,
    starting from `start`."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "add-page-numbers")

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "add-page-numbers",
            request_id,
            lambda: pdf_utilities.add_page_numbers(input_path, output_path, start),
        )
        return _pdf_response(output_path, "numbered.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/crop", dependencies=[Depends(require_api_key)])
def pdf_crop(
    request: Request,
    file: UploadFile,
    left: float = Form(0),
    top: float = Form(0),
    right: float = Form(0),
    bottom: float = Form(0),
) -> Response:
    """Crops every page inward by the given margins, in points (72 per inch)."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "crop")

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "crop",
            request_id,
            lambda: pdf_utilities.crop_pages(input_path, output_path, (left, top, right, bottom)),
        )
        return _pdf_response(output_path, "cropped.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/compress", dependencies=[Depends(require_api_key)])
def pdf_compress(request: Request, file: UploadFile) -> Response:
    """Re-saves with structural cleanup and stream compression -- see
    pdf_utilities.compress_pdf's own note on when this does and doesn't
    meaningfully shrink a file."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "compress")

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "compress", request_id, lambda: pdf_utilities.compress_pdf(input_path, output_path)
        )
        return _pdf_response(output_path, "compressed.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/repair", dependencies=[Depends(require_api_key)])
def pdf_repair(request: Request, file: UploadFile) -> Response:
    """Re-saves a PDF through PyMuPDF's own parser, repairing many
    structural issues as a side effect of successfully opening the file
    at all. A file too damaged for PyMuPDF to open at all fails at the
    upload-validation step above, before this even runs."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        # Deliberately no _check_decompression_bomb call here: a PDF
        # damaged enough to need repair may not have a reliable page
        # count to check in the first place, and the whole point of this
        # endpoint is to accept a PDF other checks might reject.
        request_id = _new_pdf_request_id(request, "repair")

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "repair", request_id, lambda: pdf_utilities.repair_pdf(input_path, output_path)
        )
        return _pdf_response(output_path, "repaired.pdf")
    finally:
        storage.release(work_dir)


# --------------------------------------------------------------------------
# Phase 12 (master directive numbering): Document Security.
# --------------------------------------------------------------------------


def _parse_redact_rects(spec: str) -> list[tuple[int, float, float, float, float]]:
    """Parses "page,left,top,right,bottom;page,left,top,right,bottom;..."
    (1-indexed page, PDF points) into pdf_security.redact_pdf's expected
    0-indexed-page rect list. Same semicolon-separates-groups convention
    as pdf_split's `ranges` parameter."""
    rects = []
    for group in spec.split(";"):
        group = group.strip()
        if not group:
            continue
        parts = [p.strip() for p in group.split(",")]
        if len(parts) != 5:
            raise ValueError(
                f"Invalid rect '{group}' -- expected 'page,left,top,right,bottom'."
            )
        try:
            page, left, top, right, bottom = (float(p) for p in parts)
        except ValueError:
            raise ValueError(f"Invalid rect '{group}' -- all five values must be numbers.") from None
        rects.append((int(page) - 1, left, top, right, bottom))
    return rects


@app.post("/api/v1/pdf/protect", dependencies=[Depends(require_api_key)])
def pdf_protect(
    request: Request,
    file: UploadFile,
    user_password: str | None = Form(None),
    owner_password: str | None = Form(None),
    permissions: str | None = Form(None),
) -> Response:
    """Encrypts the PDF (AES-256) with a user and/or owner password, and
    optionally a permission restriction (comma-separated names -- see
    pdf_security.PERMISSION_NAMES) enforced once there's an owner
    password protecting it. See pdf_security.protect_pdf's docstring for
    why at least one password is required and how owner/user passwords
    interact."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(
            request,
            "protect",
            has_user_password=bool(user_password),
            has_owner_password=bool(owner_password),
        )

        def _do() -> None:
            perm_mask = (
                pdf_security.parse_permissions(
                    [p for p in permissions.split(",") if p.strip()]
                )
                if permissions
                else None
            )
            pdf_security.protect_pdf(
                input_path,
                output_path,
                user_password=user_password,
                owner_password=owner_password,
                permissions=perm_mask,
            )

        output_path = work_dir / "output.pdf"
        _run_pdf_operation("protect", request_id, _do)
        return _pdf_response(output_path, "protected.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/unlock", dependencies=[Depends(require_api_key)])
def pdf_unlock(request: Request, file: UploadFile, password: str = Form(...)) -> Response:
    """Removes password protection from a PDF, given a password that
    successfully authenticates as either user or owner. The password
    itself is never written to the audit log."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "unlock")

        output_path = work_dir / "output.pdf"
        _run_pdf_operation(
            "unlock",
            request_id,
            lambda: pdf_security.remove_protection(input_path, output_path, password),
        )
        return _pdf_response(output_path, "unlocked.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/redact", dependencies=[Depends(require_api_key)])
def pdf_redact(
    request: Request,
    file: UploadFile,
    terms: str | None = Form(None),
    rects: str | None = Form(None),
) -> Response:
    """Permanently removes content -- not a black box drawn over
    content that is still there underneath (see pdf_security.redact_pdf's
    docstring). `terms`: comma-separated, case-sensitive search strings,
    redacted everywhere they occur. `rects`: semicolon-separated explicit
    regions, each "page,left,top,right,bottom" (1-indexed page, PDF
    points). At least one of the two is required."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "redact")

        def _do() -> int:
            term_list = [t.strip() for t in terms.split(",") if t.strip()] if terms else None
            rect_list = _parse_redact_rects(rects) if rects else None
            return pdf_security.redact_pdf(input_path, output_path, terms=term_list, rects=rect_list)

        output_path = work_dir / "output.pdf"
        _run_pdf_operation("redact", request_id, _do)
        return _pdf_response(output_path, "redacted.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/sign", dependencies=[Depends(require_api_key)])
def pdf_sign(
    request: Request,
    file: UploadFile,
    reason: str | None = Form(None),
    location: str | None = Form(None),
    field_name: str = Form("Signature1"),
    pkcs12_password: str | None = Form(None),
    pkcs12_file: UploadFile | None = File(None),
) -> Response:
    """
    Adds a real, cryptographic digital signature. Without `pkcs12_file`,
    signs with this server process's ephemeral, self-signed demo
    certificate (see pdf_security.generate_demo_signer's docstring for
    exactly what that does and doesn't prove -- tamper-evidence, not
    identity). Pass `pkcs12_file` (a .pfx/.p12 certificate+key bundle,
    with `pkcs12_password` if it's encrypted) for real, identity-bound
    signing once you have a certificate from a CA your verifiers trust.
    """
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "sign", using_custom_cert=pkcs12_file is not None)

        def _do() -> None:
            if pkcs12_file is not None:
                pkcs12_bytes = pkcs12_file.file.read()
                signer = pdf_security.load_pkcs12_signer(pkcs12_bytes, pkcs12_password)
            else:
                signer = pdf_security.generate_demo_signer()
            pdf_security.sign_pdf(
                input_path, output_path, signer, reason=reason, location=location, field_name=field_name
            )

        output_path = work_dir / "output.pdf"
        _run_pdf_operation("sign", request_id, _do)
        return _pdf_response(output_path, "signed.pdf")
    finally:
        storage.release(work_dir)


@app.post("/api/v1/pdf/verify-signatures", dependencies=[Depends(require_api_key)])
def pdf_verify_signatures(request: Request, file: UploadFile) -> dict:
    """Reports on every digital signature embedded in the PDF -- see
    pdf_security.verify_pdf's docstring for exactly what each field
    means, in particular why `trusted` is expected to be False for a
    demo-signed file. An empty `signatures` list is a normal result for
    an unsigned PDF, not an error."""
    _check_rate_limit(request)
    work_dir = storage.allocate("docconv-pdfutil-")
    try:
        input_path, ext = _require_pdf_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
        request_id = _new_pdf_request_id(request, "verify-signatures")

        signatures = _run_pdf_operation(
            "verify-signatures", request_id, lambda: pdf_security.verify_pdf(input_path)
        )
        return {"signatures": signatures}
    finally:
        storage.release(work_dir)


@app.exception_handler(Exception)
def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Catches anything not already turned into an HTTPException above (a
    # bug in this file, a Starlette-level error, etc.) so a raw traceback
    # can never reach the caller regardless of debug settings.
    print(f"Unhandled exception on {request.url.path}: {exc!r}")
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})
