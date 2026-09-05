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
