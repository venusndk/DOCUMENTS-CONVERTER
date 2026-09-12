"""
Administration & Observability (Phase 18, master directive numbering):
queue monitoring, provider monitoring, job analytics, metrics, and
audit-log querying -- the data-gathering half of every GET
/api/v1/admin/* endpoint (app.py owns the HTTP wiring, dependency
gating via accounts.get_current_admin, and the admin dashboard page
itself), same "pure logic, no FastAPI here" separation as
document_analysis.py/document_preview.py/ai_intelligence.py.

Two genuinely different kinds of number live here, on purpose:

- **Job analytics** (job_analytics()) answers "what has this service's
  own data done, historically" -- a live SQL aggregation over
  JobRecord/AuditLogRecord, the same "don't duplicate a source of
  truth" reasoning jobs.JobStore.usage_for_user already uses for one
  account's own numbers. This is for a human looking at a dashboard.

- **Metrics** (the Counter/Histogram/Gauge objects below, scraped via
  GET /metrics in Prometheus text format) answers "what is this
  process doing right now / has done since it started" -- real,
  continuously-updated instrumentation for real monitoring tooling
  (Prometheus, Grafana), not a point-in-time snapshot computed on
  request. HTTP_REQUESTS_TOTAL/HTTP_REQUEST_DURATION are updated by
  app.py's own middleware on every request; the job/queue Gauges are
  populated at scrape time (collect_runtime_gauges(), called from GET
  /metrics itself) by the same live queries queue_status()/
  job_analytics() already make -- a Gauge, not a Counter, because a
  job row can be deleted by JobStore's own retention cleanup, making
  "current count in this status" a real, legitimately-decreasing
  number, not a monotonic lifetime total this project doesn't
  currently instrument every code path enough to track correctly.
"""

from __future__ import annotations

import json

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func

from . import config, job_queue
from .db import SessionLocal, check_db_connection
from .models import AuditLogRecord, JobRecord

# --------------------------------------------------------------------------
# Prometheus metrics -- module-level singletons, updated continuously
# (HTTP ones by app.py's middleware) or at scrape time (the Gauges
# below, by collect_runtime_gauges()).
# --------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "documents_converter_http_requests_total",
    "Total HTTP requests handled, by method/path/status.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "documents_converter_http_request_duration_seconds",
    "HTTP request duration in seconds, by method/path.",
    ["method", "path"],
)
JOB_QUEUE_DEPTH = Gauge(
    "documents_converter_job_queue_depth", "Jobs currently queued, waiting for a worker."
)
JOB_QUEUE_STARTED = Gauge(
    "documents_converter_job_queue_started", "Jobs currently being processed by a worker."
)
JOB_QUEUE_FAILED = Gauge(
    "documents_converter_job_queue_failed", "Jobs in RQ's own failed-job registry."
)
JOBS_CURRENT = Gauge(
    "documents_converter_jobs_current",
    "Job rows currently in the database, by status -- NOT a lifetime "
    "total: JobStore's own retention cleanup deletes old terminal "
    "jobs, so this can legitimately decrease.",
    ["status"],
)


def collect_runtime_gauges() -> None:
    """Refreshes every Gauge above from real, live state -- called once
    at the top of GET /metrics, right before generate_latest() reads
    the registry, so a scrape never sees a stale value from the last
    time some other code path happened to touch these."""
    q = queue_status()
    if q["available"]:
        JOB_QUEUE_DEPTH.set(q["queued"])
        JOB_QUEUE_STARTED.set(q["started"])
        JOB_QUEUE_FAILED.set(q["failed"])

    with SessionLocal() as session:
        status_counts = dict(
            session.query(JobRecord.status, func.count(JobRecord.id)).group_by(JobRecord.status).all()
        )
    for status in ("queued", "processing", "completed", "failed", "cancelled"):
        JOBS_CURRENT.labels(status=status).set(status_counts.get(status, 0))


def render_metrics() -> tuple[bytes, str]:
    """Returns (body, content_type) for GET /metrics -- collects the
    live Gauges first, then dumps the whole default registry (these
    Gauges plus every HTTP counter/histogram the middleware has been
    updating) in Prometheus text exposition format."""
    collect_runtime_gauges()
    return generate_latest(), CONTENT_TYPE_LATEST


# --------------------------------------------------------------------------
# Queue monitoring
# --------------------------------------------------------------------------


def queue_status() -> dict:
    """
    Real RQ/Redis introspection -- queued/started/failed/scheduled/
    finished job counts, plus every currently-registered worker's real
    state -- not a guess or a cached/derived number. Returns
    {"available": False, "error": ...} if Redis itself is unreachable,
    the same shape every other optional-dependency check in this
    project uses (see config.py's own examples).
    """
    from rq import Worker
    from rq.registry import (
        FailedJobRegistry,
        FinishedJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    try:
        conn = job_queue.get_redis_connection()
        conn.ping()
        queue = job_queue.get_queue()
        workers = Worker.all(connection=conn)
        return {
            "available": True,
            "queued": queue.count,
            "started": StartedJobRegistry(job_queue.QUEUE_NAME, connection=conn).count,
            "failed": FailedJobRegistry(job_queue.QUEUE_NAME, connection=conn).count,
            "scheduled": ScheduledJobRegistry(job_queue.QUEUE_NAME, connection=conn).count,
            "finished": FinishedJobRegistry(job_queue.QUEUE_NAME, connection=conn).count,
            "workers": [
                {
                    "name": w.name,
                    "state": w.get_state(),
                    "successful_job_count": w.successful_job_count,
                    "failed_job_count": w.failed_job_count,
                }
                for w in workers
            ],
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


# --------------------------------------------------------------------------
# Provider monitoring
# --------------------------------------------------------------------------


def provider_status() -> dict:
    """
    Every real external dependency this project has, in one place --
    /health only ever checked Tesseract + the database (a liveness
    probe needs to stay fast and minimal); this is the fuller admin
    picture, including ones a liveness probe shouldn't block on
    (LibreOffice's own check just stats a binary path; Gemini's is a
    config-presence check, not a live API call, for the same reason
    tests/conftest.py's @requires_gemini_key never makes one either --
    it would cost real quota on every single dashboard load).
    """
    from .. import ai_intelligence
    from ..converters._libreoffice import check_libreoffice_available
    from ..ocr_excel import check_tesseract_available

    redis_available = False
    try:
        job_queue.get_redis_connection().ping()
        redis_available = True
    except Exception:
        redis_available = False

    return {
        "tesseract": check_tesseract_available(config.TESSERACT_CMD),
        "libreoffice": check_libreoffice_available(config.LIBREOFFICE_CMD),
        "redis": redis_available,
        "database": check_db_connection(),
        "ai_gemini_configured": ai_intelligence.is_ai_available(),
    }


# --------------------------------------------------------------------------
# Job analytics
# --------------------------------------------------------------------------

#: How many recent "job_completed" audit events job_analytics() reads
#: to compute an average duration -- bounded, not the whole table, so
#: this stays a cheap, fast admin-dashboard query no matter how long
#: this service has been running (see AuditLogRecord's own docstring on
#: this table having no automatic retention/pruning yet).
_DURATION_SAMPLE_SIZE = 1000


def job_analytics() -> dict:
    """
    System-wide job stats (not scoped to one account -- see
    jobs.JobStore.usage_for_user for that), computed live, the same
    "don't duplicate a source of truth" reasoning as that function.
    average_duration_ms comes from persisted "job_completed" audit
    events' own duration_ms field (audit.py/rq_tasks.py already log
    this for every real completed job) rather than a new JobRecord
    column purpose-built for it.
    """
    with SessionLocal() as session:
        status_counts = dict(
            session.query(JobRecord.status, func.count(JobRecord.id)).group_by(JobRecord.status).all()
        )
        target_counts = dict(
            session.query(JobRecord.target, func.count(JobRecord.id))
            .filter(JobRecord.target.is_not(None))
            .group_by(JobRecord.target)
            .all()
        )
        recent_completions = (
            session.query(AuditLogRecord.fields_json)
            .filter(AuditLogRecord.event == "job_completed")
            .order_by(AuditLogRecord.ts.desc())
            .limit(_DURATION_SAMPLE_SIZE)
            .all()
        )

    durations = []
    for (fields_json,) in recent_completions:
        try:
            fields = json.loads(fields_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(fields.get("duration_ms"), (int, float)):
            durations.append(fields["duration_ms"])

    total = sum(status_counts.values())
    return {
        "total_jobs": total,
        "jobs_by_status": status_counts,
        "jobs_by_target": target_counts,
        "success_rate": (status_counts.get("completed", 0) / total) if total else None,
        "average_duration_ms": (sum(durations) / len(durations)) if durations else None,
        "duration_sample_size": len(durations),
    }


# --------------------------------------------------------------------------
# Audit log querying
# --------------------------------------------------------------------------

_MAX_AUDIT_LOG_PAGE = 1000


def query_audit_log(limit: int = 100, event: str | None = None) -> list[dict]:
    """Newest first. `limit` is capped, not just defaulted, so a
    caller can't accidentally (or deliberately) pull this project's
    entire audit history in one request -- see AuditLogRecord's own
    docstring on this table having no automatic pruning yet, so it can
    genuinely grow large over a long-running deployment's lifetime."""
    limit = min(limit, _MAX_AUDIT_LOG_PAGE)
    with SessionLocal() as session:
        query = session.query(AuditLogRecord).order_by(AuditLogRecord.ts.desc())
        if event:
            query = query.filter(AuditLogRecord.event == event)
        records = query.limit(limit).all()

    result = []
    for record in records:
        try:
            fields = json.loads(record.fields_json)
        except (json.JSONDecodeError, TypeError):
            fields = {}
        result.append({"ts": record.ts, "event": record.event, **fields})
    return result
