"""
The actual function RQ workers execute (Phase 14, master directive
numbering: Job Queue & Real-Time Processing) -- looked up by dotted
import path when a job is enqueued (`documents_converter.api.rq_tasks.
run_conversion_job`), so it must be a plain, module-level function
taking only plain (str) arguments, never a Capability object or an open
file handle: RQ serializes a job by pickling its arguments into Redis,
and the worker that eventually runs it is a genuinely separate process
-- in docker-compose.yml, a separate container entirely -- that shares
state with the API process only through Redis and the filesystem each
job's work_dir lives on (see config.WORK_DIR_ROOT). Nothing here can be
passed by Python reference across that boundary; the Capability is
re-resolved fresh from `ext`/`target` inside the worker instead, the
same way every other entry point into the registry already does.
"""

from __future__ import annotations

import time
from pathlib import Path

from rq import get_current_job

from . import audit, config, security
from .jobs import JobStore

# Registers every Capability as a side effect of import -- required
# here, not just in app.py: when a worker process (worker_main.py)
# dequeues a job, RQ resolves `run_conversion_job` by dynamically
# importing *this* module by its dotted path, and this may be the
# first time that process has ever touched documents_converter at all.
# Without this import, _resolve_capability below would find an empty
# registry the first time a worker (as opposed to the API process)
# handles a job.
from .. import converters  # noqa: F401
from ..registry import registry

_job_store = JobStore(retention_seconds=config.JOB_RETENTION_SECONDS)


def _resolve_capability(ext: str, target: str):
    """Same lookup as app.py's _resolve_capability, re-implemented here
    (rather than imported from app.py) so this module never imports
    app.py -- app.py imports this module to enqueue jobs, and a worker
    process importing rq_tasks should not need to import, or stand up
    any part of, the FastAPI app itself. Raises ValueError instead of
    HTTPException; there is no HTTP response to attach one to here."""
    try:
        capability = registry.find(target, ext)
    except ValueError as e:
        raise ValueError(f"Ambiguous capability registration: {e}") from e
    if capability is None:
        known_targets = sorted({c.target_format for c in registry.list_all()})
        raise ValueError(
            f"Unsupported file type '{ext}' for target '{target}'. "
            f"Known target formats: {known_targets}."
        )
    return capability


def _report_progress(message: str) -> None:
    """Writes to the current RQ job's `meta` dict, the one piece of
    per-job state RQ itself keeps in Redis for exactly this purpose --
    GET /api/v1/jobs/{id}/events (app.py) polls it to stream progress to
    a caller in real time. A no-op outside a real RQ worker (job is
    None), which is what lets run_conversion_job also work when called
    directly/synchronously, e.g. from a test."""
    job = get_current_job()
    if job is not None:
        job.meta["progress"] = message
        job.save_meta()


def run_conversion_job(job_id: str, input_path: str, ext: str, target: str, request_id: str) -> None:
    """
    Runs one conversion. Distinguishes two failure classes, deliberately:

    - Bad input (unsupported format for `target`, a decompression-bomb
      page/pixel count, a corrupt file the capability itself rejects
      with ValueError) can never succeed no matter how many times it's
      retried -- caught here, recorded as a final "failed" JobRecord,
      and NOT re-raised, so RQ's Retry never fires for it.
    - Anything else (an environment problem, an unexpected crash) MIGHT
      succeed on a later attempt -- recorded with the current attempt's
      error and re-raised, so RQ's Retry (configured by the caller's
      queue.enqueue(..., retry=Retry(max=config.JOB_MAX_RETRIES, ...)))
      catches it and reschedules. Whether this specific attempt was the
      last one is read from the job's own `retries_left` (set by RQ at
      enqueue time, decremented after each failed attempt) rather than
      guessed at -- confirmed against a real Redis+worker run, not just
      assumed from the library's docs (see README's Job Queue section
      for the specific retry-then-succeed and retries-exhausted
      sequences that were actually exercised).
    """
    _job_store.update(job_id, status="processing")
    start_time = time.monotonic()
    input_path_obj = Path(input_path)

    def _failed(reason: str) -> None:
        audit.log_event(
            "job_failed",
            request_id=request_id,
            job_id=job_id,
            reason=reason,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )

    try:
        capability = _resolve_capability(ext, target)
        output_path = input_path_obj.parent / f"output{capability.output_extension}"
        _job_store.update(
            job_id,
            result_media_type=capability.media_type,
            result_filename=f"converted{capability.output_extension}",
        )
        security.check_decompression_bomb(input_path_obj, ext)
        _report_progress("converting")
        capability.convert(input_path_obj, output_path, progress=_report_progress)

        review_path = output_path.parent / f"{output_path.stem}.review.json"
        _job_store.update(
            job_id,
            status="completed",
            result_path=output_path,
            review_path=review_path if review_path.exists() else None,
            # Clears whatever a previous, since-retried attempt may have
            # recorded (see the "will_retry" branch below) -- a job that
            # failed once and then succeeded on retry is genuinely
            # completed, and must not still show that earlier attempt's
            # error message. Caught by testing the retry-then-succeed
            # path for real, not assumed: the first version of this
            # function left the stale error in place.
            error=None,
        )
        audit.log_event(
            "job_completed",
            request_id=request_id,
            job_id=job_id,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )
    except ValueError as e:
        # Bad input -- final, never retried (see docstring above).
        # FileTooLargeError is itself a ValueError subclass (security.py),
        # so it's caught here too; checked first since every
        # FileTooLargeError is also a ValueError but not vice versa.
        _job_store.update(job_id, status="failed", error=str(e))
        _failed("file_too_large" if isinstance(e, security.FileTooLargeError) else "invalid_input")
    except Exception as e:
        current_job = get_current_job()
        will_retry = current_job is not None and (current_job.retries_left or 0) > 0
        message = (
            "Conversion failed and will be retried."
            if will_retry
            else "Conversion failed. This has been logged for investigation."
        )
        print(f"[{request_id}] job {job_id} failed (retry pending={will_retry}): {e!r}")
        _job_store.update(
            job_id, status="queued" if will_retry else "failed", error=message
        )
        _failed("will_retry" if will_retry else "internal_error")
        raise
