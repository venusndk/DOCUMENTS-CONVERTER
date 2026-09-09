"""
Minimal environment-based configuration for the API layer.

Per docs/PHASE_0_AUDIT.md's own advice (never hardcode secrets/paths; use
env vars), and appropriately small for Phase 3's scope -- this is not the
full config system a later "production foundation" phase might build, just
enough for this one API to run without hardcoded machine-specific paths.
"""

from __future__ import annotations

import os

# Full path to the tesseract executable, only needed if it's not already on
# PATH (mirrors the CLI's --tesseract-cmd). Unset by default.
TESSERACT_CMD: str | None = os.environ.get("TESSERACT_CMD") or None

# Full path to the LibreOffice (soffice) executable, only needed if it's
# not already on PATH -- same convention as TESSERACT_CMD above. Used by
# the Office/HTML/Markdown -> PDF capabilities (master directive Phase 9,
# documents_converter/converters/_libreoffice.py). Unset by default.
LIBREOFFICE_CMD: str | None = os.environ.get("LIBREOFFICE_CMD") or None

# Reject uploads above this size before doing any processing work.
MAX_UPLOAD_MB: int = int(os.environ.get("MAX_UPLOAD_MB", "50"))

# Which extensions are accepted for upload used to be a fixed list here.
# Phase 9 replaced it: what's "allowed" now depends on which target format
# the caller asked for, answered by the capability registry
# (documents_converter/registry.py, routed via app.py's
# _resolve_capability) instead of one global set -- see GET
# /api/v1/capabilities for the live answer.

# Phase 6 (docs/PHASE_0_AUDIT.md): API-key auth. Comma-separated list of
# accepted keys. Empty by default -- auth is OFF until at least one key is
# configured, a deliberate choice (not a silent bypass) so a fresh local
# dev setup keeps working with zero extra configuration. Set this before
# exposing the API to anything other than trusted local use.
API_KEYS: tuple[str, ...] = tuple(
    k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()
)

# Phase 4 (docs/PHASE_0_AUDIT.md): security hardening.
RATE_LIMIT_MAX_REQUESTS: int = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "10"))
RATE_LIMIT_WINDOW_SECONDS: float = float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
# Best-effort wall-clock cap per conversion. "Best-effort" because a
# ThreadPoolExecutor future can be abandoned on timeout but the underlying
# OS thread isn't forcibly killed (Python has no safe API for that) -- see
# app.py. Real documents in this project's own testing took well under a
# minute; generous enough to allow that with margin for a slow machine.
CONVERT_TIMEOUT_SECONDS: float = float(os.environ.get("CONVERT_TIMEOUT_SECONDS", "180"))

# Phase 7 (docs/PHASE_0_AUDIT.md): async job queue. How long a finished
# (completed or failed) job's result stays downloadable before its temp
# files are cleaned up.
JOB_RETENTION_SECONDS: float = float(os.environ.get("JOB_RETENTION_SECONDS", "3600"))

# Phase 11 (docs/PHASE_0_AUDIT.md numbering continued): production
# readiness. "development" (the default) never blocks startup no matter
# how it's configured -- a fresh local checkout must keep working with
# zero setup. Set to "production" to make app.py refuse to start with
# auth off (see app._check_startup_config): a real hosted deployment of
# this service (per the project's own stated direction -- see README)
# should not be able to go live unauthenticated by omission.
ENVIRONMENT: str = os.environ.get("ENVIRONMENT", "development")

# Phase 14 (master directive numbering): Job Queue & Real-Time
# Processing. Redis-backed job queue (RQ) replacing the in-process
# ThreadPoolExecutor for /api/v1/jobs and /api/v1/batch -- a real,
# separate worker process (documents_converter/api/worker_main.py,
# docker-compose.yml's `worker` service) pulls jobs off this queue
# instead of the API process running conversions on its own threads.
# Local/dev default matches this project's other infra choices (SQLite
# default DB, no LibreOffice on the dev machine): a fresh checkout with
# no Redis running simply can't exercise the queue (see
# tests/conftest.py's @requires_redis), same shape as the Tesseract/
# LibreOffice gaps -- real verification happens in Docker Compose/CI,
# not by silently faking it in-process.
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# How many times RQ automatically retries a job that failed for a
# transient/environment reason (not a bad-input error -- see
# rq_tasks.py's run_conversion_job for that distinction). Retries use a
# short backoff (rq_tasks.RETRY_INTERVALS), not immediate resubmission.
JOB_MAX_RETRIES: int = int(os.environ.get("JOB_MAX_RETRIES", "2"))

# When set, every job/batch/sync-conversion work directory is created
# under this path instead of the OS default temp directory
# (documents_converter/api/storage.py). Unset (the default) preserves
# this project's original single-process behavior exactly. Required
# once the API and worker are separate processes with separate
# filesystems (different Docker containers, in particular): a worker
# pulling a job off Redis has no access to a temp directory the API
# container created under its own /tmp, so docker-compose.yml points
# both containers' WORK_DIR_ROOT at the same shared volume.
WORK_DIR_ROOT: str | None = os.environ.get("WORK_DIR_ROOT") or None

# Phase 11: minimal audit trail (documents_converter/api/audit.py). Unset
# by default -- the audit log always goes to stdout regardless (captured
# by whatever log aggregation a real deployment already has); set this to
# also append it to a file, e.g. on a mounted volume, for a deployment
# that wants that record to outlive the container without standing up a
# database just for this.
AUDIT_LOG_PATH: str | None = os.environ.get("AUDIT_LOG_PATH") or None

# Phase 1 completion (master directive numbering -- see
# documents_converter/api/db.py): the project's persistence layer.
# Defaults to a local SQLite file so a fresh checkout keeps working with
# zero extra infrastructure, same as every other default in this file;
# point this at a real Postgres instance (e.g.
# "postgresql+psycopg://user:pass@host:5432/dbname") for a deployment
# that needs jobs to survive a restart and be visible across more than
# one replica. Both are real, tested backends -- not "SQLite for dev,
# Postgres someday" -- see tests/test_jobs_db.py.
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL", "sqlite:///./data/documents_converter.db"
)
