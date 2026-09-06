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
and the caller polls for status. See jobs.py for the in-memory job store
and its own documented single-process limitation. Both paths share the
same validation (_validate_and_save_upload, _check_decompression_bomb)
and the same worker pool (_convert_executor) -- documented as a known
limitation in docs/PHASE_0_AUDIT.md: heavy async job load could delay
sync requests, since they compete for the same 4 worker threads.

Phase 4 added security hardening on top of Phase 3's basic hygiene
(extension allowlist, safe temp-file naming, no document-content
logging): magic-byte validation, decompression-bomb limits, per-IP rate
limiting, a best-effort conversion timeout, and a catch-all exception
handler so nothing unexpected ever leaks a stack trace to the caller.

Phase 6 added API-key authentication on every /api/v1/* route (see
auth.py) -- disabled by default until config.API_KEYS is set, so
local/dev use needs no extra setup. /health stays unauthenticated on
purpose: load balancers and monitoring probes need to reach it without
credentials.

Run locally:
    uvicorn documents_converter.api.app:app --reload
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image as PILImage

import fitz

from . import config, security
from .auth import require_api_key
from .jobs import Job, JobStore
from .rate_limit import FixedWindowRateLimiter
from ..ocr_excel import check_tesseract_available, convert_scanned_to_excel

app = FastAPI(title="Documents Converter API", version="0.1.0")

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

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
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
    service whose real dependency is an external binary.
    """
    return {
        "status": "ok",
        "tesseract_available": check_tesseract_available(config.TESSERACT_CMD),
    }


def _check_rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")


def _validate_and_save_upload(request: Request, file: UploadFile, work_dir: Path) -> tuple[Path, str]:
    """
    Shared by both the sync and async endpoints: validates the extension,
    upload size, and magic-byte signature while streaming the upload to
    `work_dir`. Raises HTTPException on any validation failure.
    :return: (input_path, ext)
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: "
            f"{sorted(config.ALLOWED_EXTENSIONS)}",
        )

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


@app.post("/api/v1/convert", dependencies=[Depends(require_api_key)])
def convert(request: Request, file: UploadFile) -> Response:
    """
    Accepts one scanned PDF or image, returns the extracted table(s) as an
    .xlsx file. Synchronous: the response is the finished file, not a job
    reference. See /api/v1/jobs for the async alternative.
    """
    _check_rate_limit(request)

    with tempfile.TemporaryDirectory(prefix="docconv-") as tmp_dir:
        input_path, ext = _validate_and_save_upload(request, file, Path(tmp_dir))
        output_path = Path(tmp_dir) / "output.xlsx"

        request_id = uuid.uuid4().hex[:12]
        # Safe metadata only (docs/PHASE_0_AUDIT.md: never log document
        # contents) -- extension and size, not the client-supplied filename
        # or anything read from inside the file.
        print(f"[{request_id}] convert request: ext={ext} size={input_path.stat().st_size}B")

        try:
            _check_decompression_bomb(input_path, ext)
            future = _convert_executor.submit(
                convert_scanned_to_excel,
                file_path=str(input_path),
                output_excel_path=str(output_path),
                tesseract_cmd=config.TESSERACT_CMD,
                progress=lambda msg: print(f"[{request_id}] {msg}"),
            )
            future.result(timeout=config.CONVERT_TIMEOUT_SECONDS)
        except security.FileTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e)) from e
        except FutureTimeoutError as e:
            raise HTTPException(
                status_code=504,
                detail=f"Conversion exceeded {config.CONVERT_TIMEOUT_SECONDS:.0f}s and was abandoned.",
            ) from e
        except EnvironmentError as e:
            # Tesseract missing/misconfigured -- a server problem, not a
            # bad request.
            raise HTTPException(status_code=503, detail=str(e)) from e
        except Exception as e:
            # Per docs/PHASE_0_AUDIT.md failure philosophy: never expose a
            # raw stack trace to the caller. Log the real error server-side
            # (safe: this is a pipeline error, not document content) and
            # return a generic message.
            print(f"[{request_id}] conversion failed: {e!r}")
            raise HTTPException(
                status_code=500,
                detail="Conversion failed. This has been logged for investigation.",
            ) from e

        xlsx_bytes = output_path.read_bytes()

    return Response(
        content=xlsx_bytes,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": "attachment; filename=converted.xlsx"},
    )


def _run_job(job: Job, input_path: Path, output_path: Path, ext: str, request_id: str) -> None:
    """Runs on the shared executor, in the background -- the HTTP request
    that created this job has already returned by the time this runs."""
    _job_store.update(job.id, status="processing")
    try:
        _check_decompression_bomb(input_path, ext)
        convert_scanned_to_excel(
            file_path=str(input_path),
            output_excel_path=str(output_path),
            tesseract_cmd=config.TESSERACT_CMD,
            progress=lambda msg: print(f"[{request_id}] {msg}"),
        )
        _job_store.update(job.id, status="completed", result_path=output_path)
    except security.FileTooLargeError as e:
        _job_store.update(job.id, status="failed", error=str(e))
    except EnvironmentError as e:
        _job_store.update(job.id, status="failed", error=str(e))
    except Exception as e:
        # Same failure philosophy as the sync endpoint: log the real error
        # server-side, expose only a generic message via the status endpoint.
        print(f"[{request_id}] job {job.id} failed: {e!r}")
        _job_store.update(
            job.id, status="failed", error="Conversion failed. This has been logged for investigation."
        )


@app.post("/api/v1/jobs", dependencies=[Depends(require_api_key)], status_code=202)
def create_job(request: Request, file: UploadFile) -> dict:
    """
    Accepts one scanned PDF or image, validates it synchronously (so bad
    input is rejected immediately, not discovered later by polling), then
    queues the actual OCR conversion in the background and returns right
    away with a job id to poll.
    """
    _check_rate_limit(request)

    job = _job_store.create()
    work_dir = Path(tempfile.mkdtemp(prefix=f"docconv-job-{job.id}-"))
    job.work_dir = work_dir

    try:
        input_path, ext = _validate_and_save_upload(request, file, work_dir)
        _check_decompression_bomb(input_path, ext)
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        _job_store.update(job.id, status="failed")
        raise
    except security.FileTooLargeError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=413, detail=str(e)) from e

    output_path = work_dir / "output.xlsx"
    request_id = uuid.uuid4().hex[:12]
    print(f"[{request_id}] job {job.id} queued: ext={ext} size={input_path.stat().st_size}B")

    _convert_executor.submit(_run_job, job, input_path, output_path, ext, request_id)

    return {"job_id": job.id, "status": job.status}


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job_status(job_id: str) -> dict:
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    body = {"job_id": job.id, "status": job.status}
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
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": "attachment; filename=converted.xlsx"},
    )


@app.exception_handler(Exception)
def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Catches anything not already turned into an HTTPException above (a
    # bug in this file, a Starlette-level error, etc.) so a raw traceback
    # can never reach the caller regardless of debug settings.
    print(f"Unhandled exception on {request.url.path}: {exc!r}")
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})
