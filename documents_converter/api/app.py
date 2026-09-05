"""
Minimal synchronous HTTP API around convert_scanned_to_excel.

Deliberately NOT a job queue: the conversion runs inline and the response
holds the connection open until it's done. That's the right amount of
infrastructure for Phase 3 (see docs/PHASE_0_AUDIT.md) -- a queue only
earns its complexity once real usage shows requests taking long enough to
need it. The endpoint is defined as a plain `def` (not `async def`) so
FastAPI/Starlette runs it in its worker threadpool rather than blocking
the event loop, which is the correct middle ground for CPU/IO-bound work
without building a full async job system yet.

Phase 4 added security hardening on top of Phase 3's basic hygiene
(extension allowlist, safe temp-file naming, no document-content
logging): magic-byte validation, decompression-bomb limits, per-IP rate
limiting, a best-effort conversion timeout, and a catch-all exception
handler so nothing unexpected ever leaks a stack trace to the caller.

Run locally:
    uvicorn documents_converter.api.app:app --reload
"""

from __future__ import annotations

import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image as PILImage

import fitz

from . import config, security
from .rate_limit import FixedWindowRateLimiter
from ..ocr_excel import check_tesseract_available, convert_scanned_to_excel

app = FastAPI(title="Documents Converter API", version="0.1.0")

_rate_limiter = FixedWindowRateLimiter(
    max_requests=config.RATE_LIMIT_MAX_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
)
# Shared across requests rather than one-per-request: this is what
# actually gives convert_scanned_to_excel a wall-clock timeout (see
# module docstring -- "best-effort" because Python has no safe API to
# force-kill a thread; an abandoned one keeps running until it finishes
# on its own, it's just no longer waited on).
_convert_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="convert")


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


@app.post("/api/v1/convert")
def convert(request: Request, file: UploadFile) -> Response:
    """
    Accepts one scanned PDF or image, returns the extracted table(s) as an
    .xlsx file. Synchronous: the response is the finished file, not a job
    reference (see module docstring).
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")

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
    # read into an isolated temp directory under a fixed, safe name.
    with tempfile.TemporaryDirectory(prefix="docconv-") as tmp_dir:
        input_path = Path(tmp_dir) / f"input{ext}"
        output_path = Path(tmp_dir) / "output.xlsx"

        size = 0
        header_checked = False
        with open(input_path, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
                if not header_checked:
                    if not security.matches_magic_bytes(
                        ext, chunk[: security.MAGIC_BYTES_TO_READ]
                    ):
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

        request_id = uuid.uuid4().hex[:12]
        # Safe metadata only (docs/PHASE_0_AUDIT.md: never log document
        # contents) -- extension and size, not the client-supplied filename
        # or anything read from inside the file.
        print(f"[{request_id}] convert request: ext={ext} size={size}B")

        try:
            # Decompression-bomb guard: check the parsed/decompressed size
            # before running the full (much more expensive) OCR pipeline.
            if ext == ".pdf":
                doc = fitz.open(str(input_path))
                n_pages = len(doc)
                doc.close()
                security.check_pdf_page_count(n_pages)
            else:
                with PILImage.open(input_path) as img:
                    security.check_image_dimensions(*img.size)

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
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=converted.xlsx"},
    )


@app.exception_handler(Exception)
def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Catches anything not already turned into an HTTPException above (a
    # bug in this file, a Starlette-level error, etc.) so a raw traceback
    # can never reach the caller regardless of debug settings.
    print(f"Unhandled exception on {request.url.path}: {exc!r}")
    return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})
