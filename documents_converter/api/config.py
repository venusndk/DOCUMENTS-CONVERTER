"""
Minimal environment-based configuration for the API layer.

Per docs/PHASE_0_AUDIT.md's own advice (never hardcode secrets/paths; use
env vars), and appropriately small for Phase 3's scope -- this is not the
full config system a later "production foundation" phase might build, just
enough for this one API to run without hardcoded machine-specific paths.
"""

from __future__ import annotations

import os
import secrets

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

# Phase 16 (master directive numbering): AI Document Intelligence --
# vision fallback, structured extraction, summarization, translation,
# intelligent classification (documents_converter/ai_intelligence.py).
# Explicitly "optional/configurable" per the directive: unset by
# default, same as every other optional external dependency in this
# file, and every /api/v1/ai/* endpoint checks ai_intelligence.
# is_ai_available() first and returns a clear 503 rather than failing
# unpredictably deep inside a request.
#
# Google Gemini, not Anthropic/Claude -- a deliberate, disclosed
# departure from this environment's own "default to the latest Claude
# models" guidance, made for a concrete, real reason: Gemini's free
# tier needs no billing/credit card at all, while a working Anthropic
# key with zero credits purchased genuinely cannot make a single
# request (confirmed directly: a real call against a real, valid
# Anthropic key failed with "Your credit balance is too low" before
# any Phase 16 code existed). For a project whose own stated mission is
# usability without ongoing cost to whoever runs it, that difference
# decided the provider.
GEMINI_API_KEY: str | None = os.environ.get("GEMINI_API_KEY") or None

# Real, empirically-confirmed limitation of that free tier, found while
# verifying this phase: Gemini caps AI_MODEL at 20 requests/DAY per
# project on the free tier (the API's own 429 response names this
# explicitly -- quotaId=GenerateRequestsPerDayPerProjectPerModel-
# FreeTier, quotaValue=20). A real constraint on whoever runs this
# service with a free key, not just this project's own tests -- worth
# knowing before relying on these endpoints for real traffic. A paid
# Gemini plan raises this considerably; this project doesn't require one.

# gemini-2.5-flash -- the obvious first guess -- turned out to already
# be retired for new accounts by the time this phase was built ("this
# model is no longer available to new users"); confirmed against the
# real API, not assumed from a cached model list, and the error message
# itself named this replacement.
AI_MODEL: str = os.environ.get("AI_MODEL", "gemini-3.6-flash")

# A caller pasting or forwarding an entire large document as `text` to
# summarize/translate/classify/extract would otherwise pay for (and
# wait on) far more tokens than any of these features actually need to
# work well -- bounded the same way document_preview.py's own text
# preview is, for the same reason.
AI_MAX_TEXT_CHARS: int = int(os.environ.get("AI_MAX_TEXT_CHARS", "20000"))

# Phase 17 (master directive numbering): Authentication & User
# Workspace. Real accounts (documents_converter/api/accounts.py) --
# separate from, and additive to, auth.py's existing pre-shared-key
# access control above: API_KEYS decides *whether a request is let in
# at all*; this decides *whose workspace a request acts on* (history,
# preferences, usage, saved jobs). Neither replaces the other -- an
# unauthenticated or API-key-only caller keeps working exactly as
# before, with no account attached to its jobs, the same "additive,
# opt-in" shape as Phase 16's AI features.
#
# Server-side session cookies, not a JWT -- a deliberate, user-made
# choice (this project's own dual audience, a browser page and script/
# API callers, made either reasonable; a real login UI tipped it toward
# cookies). A cookie-based session needs real CSRF protection, unlike a
# bearer token a script attaches by hand -- see accounts.py's own
# docstring for the signed-double-submit-cookie scheme built for that,
# keyed by this secret.
#
# No real default is safe to ship: an ephemeral, randomly-generated
# secret is used when this is unset, so local/dev use needs no extra
# setup (same "fresh checkout works with zero configuration" bar as
# every other default in this file) -- but it also means every already
# logged-in browser's CSRF token stops validating the moment this
# process restarts (the session row itself is still valid; only the
# HMAC used to check the X-CSRF-Token header changes), forcing a fresh
# login. `_check_startup_config` warns the same way it already does for
# an unset API_KEYS in production; a real deployment should set this
# explicitly so it survives a restart/redeploy.
SESSION_SECRET: str = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
# Lets app.py's startup guard warn specifically about the ephemeral-
# fallback case above, in production -- SESSION_SECRET itself is always
# a real string by the time anything reads it, so this is the only way
# to tell "the caller actually set this" from "this process invented
# one just now."
SESSION_SECRET_WAS_SET: bool = bool(os.environ.get("SESSION_SECRET"))

# How long a session stays valid after login, with no sliding renewal
# on activity -- simpler and more predictable than a "still active
# resets the clock" scheme, at the cost of a session that's used every
# day still expiring on a fixed schedule. 14 days by default: long
# enough that "log in again" isn't an everyday annoyance for this
# project's own actual usage pattern (an occasional document-conversion
# session, not a service someone is logged into continuously), short
# enough that a stolen/forgotten session cookie doesn't stay valid
# indefinitely.
SESSION_MAX_AGE_SECONDS: float = float(os.environ.get("SESSION_MAX_AGE_SECONDS", str(14 * 24 * 3600)))
