"""
Async job store for the background conversion queue (Phase 7,
docs/PHASE_0_AUDIT.md), now backed by a real database (Phase 1
completion, master directive numbering) instead of an in-memory dict.

Jobs now survive a process restart, and -- when config.DATABASE_URL
points at a shared Postgres instance rather than the local SQLite
default -- are visible across multiple replicas too. That was the
specific, long-documented limitation of the original in-memory version
(flagged in this module since Phase 7 and repeated in every phase's
commit message that touched it since); it's the reason the job store,
not some other table, is the first thing this project puts a database
under.

The public interface (Job, JobStore.create/get/update) is unchanged
from the original in-memory version on purpose -- app.py's code was
already written against this shape and needed zero changes to pick up
real persistence.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .db import SessionLocal
from .models import JobRecord
from .storage import storage

JobStatus = Literal["queued", "processing", "completed", "failed"]


@dataclass
class Job:
    """In-memory view of one job row, handed back to callers. app.py's
    code is written against this shape, not the SQLAlchemy model
    directly, so the storage backend stays swappable behind it."""

    id: str
    status: JobStatus = "queued"
    created_at: float = 0.0
    error: str | None = None
    result_path: Path | None = None
    result_media_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    result_filename: str = "converted.xlsx"
    # The job's own temp directory (holds input + output). Owned by the
    # job, not auto-cleaned on scope exit like the sync endpoint's -- a
    # job may be polled and its result downloaded well after the request
    # that created it has returned, so cleanup happens on a retention
    # timer (JobStore._cleanup_expired) instead.
    work_dir: Path | None = None
    # Set only for jobs whose capability produced a review artifact
    # (currently just OCR->Excel -- see converters/ocr_to_excel.py).
    # None for every other capability's jobs. Master directive Phase 7
    # completion's "preview"/"human review" support.
    review_path: Path | None = None

    @classmethod
    def _from_record(cls, record: JobRecord) -> Job:
        return cls(
            id=record.id,
            status=record.status,
            created_at=record.created_at,
            error=record.error,
            result_path=Path(record.result_path) if record.result_path else None,
            result_media_type=record.result_media_type,
            result_filename=record.result_filename,
            work_dir=Path(record.work_dir) if record.work_dir else None,
            review_path=Path(record.review_path) if record.review_path else None,
        )


class JobStore:
    def __init__(self, retention_seconds: float):
        self.retention_seconds = retention_seconds

    def create(self) -> Job:
        self._cleanup_expired()
        job_id = uuid.uuid4().hex
        now = time.time()
        with SessionLocal() as session:
            session.add(JobRecord(id=job_id, status="queued", created_at=now))
            session.commit()
        return Job(id=job_id, status="queued", created_at=now)

    def get(self, job_id: str) -> Job | None:
        with SessionLocal() as session:
            record = session.get(JobRecord, job_id)
            return Job._from_record(record) if record is not None else None

    def update(self, job_id: str, **fields) -> None:
        with SessionLocal() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return
            for key, value in fields.items():
                # The Job dataclass holds Path objects for result_path/
                # work_dir; the DB column is plain text.
                if isinstance(value, Path):
                    value = str(value)
                setattr(record, key, value)
            session.commit()

    def _cleanup_expired(self) -> None:
        """Lazily removes finished jobs (and their temp directories) past
        the retention window. Called on every create() rather than run on
        a separate timer thread -- simple, and sufficient for this
        project's actual load pattern (no long idle periods between
        jobs in practice)."""
        cutoff = time.time() - self.retention_seconds
        with SessionLocal() as session:
            expired = (
                session.query(JobRecord)
                .filter(JobRecord.status.in_(["completed", "failed"]))
                .filter(JobRecord.created_at < cutoff)
                .all()
            )
            for record in expired:
                if record.work_dir:
                    storage.release(Path(record.work_dir))
                session.delete(record)
            session.commit()
