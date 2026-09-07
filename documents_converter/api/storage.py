"""
Storage abstraction for per-request/per-job scratch workspaces (master
directive Phase 3, "File Ingestion & Security" -- the one gap left in
that phase after upload/magic-byte/size validation and isolated
per-request temp storage, all built in earlier phases).

Every current file consumer -- Tesseract (invoked as a subprocess),
PyMuPDF, PIL, img2table -- reads and writes real local filesystem paths,
not abstract byte streams, so this abstraction's actual job is
centralizing "how does this service get an isolated scratch directory,
and how does it reclaim one" behind one seam, instead of
tempfile.mkdtemp()/TemporaryDirectory() and shutil.rmtree() calls
scattered across app.py and jobs.py. It deliberately does NOT hide file
I/O behind a read/write-bytes interface: every real backend, including a
hypothetical future one that also syncs to S3, would still need to
stage through a real local directory for these tools to operate on, so
that's what the seam deals in.

One implementation exists: LocalDiskStorage, exactly the OS-temp-directory
behavior the calls it replaces already had -- this is a refactor of
already-correct code behind a seam, not a new capability. A second
backend is a reasonable later addition once there's an actual need (e.g.
job results outliving a single container without relying on
DATABASE_URL-adjacent volume mounts) -- not built speculatively now,
matching docs/PHASE_0_AUDIT.md's standing advice against premature
infrastructure.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Protocol


class StorageBackend(Protocol):
    """Allocates and releases the scratch directory each upload,
    synchronous conversion, or async job actually reads and writes real
    files in.

    Both methods deal in plain local filesystem Paths -- Job records
    (documents_converter/api/jobs.py, models.py) persist `work_dir` as a
    path string in the database, so a future backend that also syncs to
    remote storage would still allocate/release a real local staging
    path here, just with extra upload/download work wrapped around it,
    not a different call shape callers need to account for.
    """

    def allocate(self, prefix: str) -> Path: ...
    def release(self, path: Path) -> None: ...


class LocalDiskStorage:
    """The only backend this service currently ships."""

    def allocate(self, prefix: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=prefix))

    def release(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)


# Module-level singleton, same pattern as app.py's _job_store/_rate_limiter
# and registry.py's `registry` -- one shared instance, swappable in tests
# via monkeypatch.setattr if a test ever needs a fake backend.
storage: StorageBackend = LocalDiskStorage()
