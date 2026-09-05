"""
In-memory async job store for the background conversion queue (Phase 7,
docs/PHASE_0_AUDIT.md).

Deliberately not Redis or a database -- consistent with rate_limit.py's
own documented limitation: correct for the single-process deployment
this project currently is, but jobs don't survive a process restart and
aren't visible across multiple replicas. A real multi-instance deployment
needs a shared job store instead, tracked as a known limitation rather
than quietly assumed away.
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal

JobStatus = Literal["queued", "processing", "completed", "failed"]


@dataclass
class Job:
    id: str
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    error: str | None = None
    result_path: Path | None = None
    # The job's own temp directory (holds input + output). Owned by the
    # job, not auto-cleaned on scope exit like the sync endpoint's -- a
    # job may be polled and its result downloaded well after the request
    # that created it has returned, so cleanup happens on a retention
    # timer (JobStore._cleanup_expired) instead.
    work_dir: Path | None = None


class JobStore:
    def __init__(self, retention_seconds: float):
        self.retention_seconds = retention_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def create(self) -> Job:
        self._cleanup_expired()
        job = Job(id=uuid.uuid4().hex)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def _cleanup_expired(self) -> None:
        """Lazily removes finished jobs (and their temp directories) past
        the retention window. Called on every create() rather than run on
        a separate timer thread -- simple, and sufficient for a
        single-process deployment with no long-running idle periods
        between jobs."""
        now = time.time()
        with self._lock:
            expired_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.status in ("completed", "failed")
                and now - job.created_at > self.retention_seconds
            ]
            expired_jobs = [self._jobs.pop(job_id) for job_id in expired_ids]

        for job in expired_jobs:
            if job.work_dir and job.work_dir.exists():
                shutil.rmtree(job.work_dir, ignore_errors=True)
