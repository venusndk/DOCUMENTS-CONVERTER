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

# Reject uploads above this size before doing any processing work.
MAX_UPLOAD_MB: int = int(os.environ.get("MAX_UPLOAD_MB", "50"))

# Extensions accepted for upload. Matches ocr_excel.IMAGE_EXTENSIONS plus PDF.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
)

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
