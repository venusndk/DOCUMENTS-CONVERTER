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

from sqlalchemy import func

from .db import SessionLocal
from .models import JobRecord
from .storage import storage

JobStatus = Literal["queued", "processing", "completed", "failed", "cancelled"]


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
    # Phase 14 (master directive numbering): the `target` this job was
    # queued against -- see JobRecord.target's own docstring for why
    # retry/resume need it.
    target: str | None = None
    # Phase 17 (master directive numbering): see JobRecord.user_id's and
    # .saved's own docstrings (models.py).
    user_id: str | None = None
    saved: bool = False

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
            target=record.target,
            user_id=record.user_id,
            saved=record.saved,
        )


class JobStore:
    def __init__(self, retention_seconds: float):
        self.retention_seconds = retention_seconds

    def create(self, target: str | None = None, user_id: str | None = None) -> Job:
        self._cleanup_expired()
        job_id = uuid.uuid4().hex
        now = time.time()
        with SessionLocal() as session:
            session.add(
                JobRecord(id=job_id, status="queued", created_at=now, target=target, user_id=user_id)
            )
            session.commit()
        return Job(id=job_id, status="queued", created_at=now, target=target, user_id=user_id)

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
        jobs in practice).

        Phase 17 (master directive numbering) added one exemption:
        `saved` jobs are never swept here, regardless of age -- see
        JobRecord.saved's own docstring for why that's a per-job,
        caller-controlled opt-in rather than "every logged-in user's
        job lives forever" (which would make storage growth an
        unbounded, automatic liability of merely being logged in)."""
        cutoff = time.time() - self.retention_seconds
        with SessionLocal() as session:
            expired = (
                session.query(JobRecord)
                .filter(JobRecord.status.in_(["completed", "failed", "cancelled"]))
                .filter(JobRecord.created_at < cutoff)
                .filter(JobRecord.saved.is_(False))
                .all()
            )
            for record in expired:
                if record.work_dir:
                    storage.release(Path(record.work_dir))
                session.delete(record)
            session.commit()

    def list_for_user(self, user_id: str, limit: int = 200) -> list[Job]:
        """Phase 17 (master directive numbering): a logged-in user's own
        job history, newest first -- both saved and unsaved (the `saved`
        flag only ever affects retention, not visibility). `limit`
        matches this project's existing "no pagination yet" bar
        elsewhere (e.g. batch listing) -- capped rather than unbounded,
        since a very active account's full history has no real use case
        yet that justifies building real cursor pagination for it."""
        with SessionLocal() as session:
            records = (
                session.query(JobRecord)
                .filter(JobRecord.user_id == user_id)
                .order_by(JobRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            return [Job._from_record(r) for r in records]

    def usage_for_user(self, user_id: str) -> dict:
        """
        Phase 17 (master directive numbering): GET /api/v1/account/usage's
        real numbers -- computed live with a real SQL GROUP BY, not a
        separate running counter that could drift from what actually
        happened (the same "don't duplicate a source of truth" reasoning
        document_analysis.py's own docstring gives for its quality
        flags). Cheap enough for this project's actual scale that a real
        query beats maintaining a second one just to avoid it.
        """
        with SessionLocal() as session:
            status_counts = dict(
                session.query(JobRecord.status, func.count(JobRecord.id))
                .filter(JobRecord.user_id == user_id)
                .group_by(JobRecord.status)
                .all()
            )
            saved_count = (
                session.query(func.count(JobRecord.id))
                .filter(JobRecord.user_id == user_id, JobRecord.saved.is_(True))
                .scalar()
            )
        return {
            "total_jobs": sum(status_counts.values()),
            "jobs_by_status": status_counts,
            "saved_jobs": saved_count or 0,
        }

    def set_saved(self, job_id: str, saved: bool) -> Job | None:
        with SessionLocal() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return None
            record.saved = saved
            session.commit()
            return Job._from_record(record)
