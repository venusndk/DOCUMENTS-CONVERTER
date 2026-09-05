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

Run locally:
    uvicorn documents_converter.api.app:app --reload
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response

from . import config
from ..ocr_excel import check_tesseract_available, convert_scanned_to_excel

app = FastAPI(title="Documents Converter API", version="0.1.0")


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
def convert(file: UploadFile) -> Response:
    """
    Accepts one scanned PDF or image, returns the extracted table(s) as an
    .xlsx file. Synchronous: the response is the finished file, not a job
    reference (see module docstring).
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: "
            f"{sorted(config.ALLOWED_EXTENSIONS)}",
        )

    # Never trust the client-supplied filename for path construction --
    # read into an isolated temp directory under a fixed, safe name.
    with tempfile.TemporaryDirectory(prefix="docconv-") as tmp_dir:
        input_path = Path(tmp_dir) / f"input{ext}"
        output_path = Path(tmp_dir) / "output.xlsx"

        size = 0
        max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
        with open(input_path, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
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
            convert_scanned_to_excel(
                file_path=str(input_path),
                output_excel_path=str(output_path),
                tesseract_cmd=config.TESSERACT_CMD,
                progress=lambda msg: print(f"[{request_id}] {msg}"),
            )
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
