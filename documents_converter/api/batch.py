"""
Batch job store (Phase 13, master directive numbering: Batch
Processing) -- database-backed from day one, unlike JobStore's original
in-memory version, since there is no reason to repeat that migration a
second time now that the pattern (and the database) already exist.

Deliberately thin: a BatchRecord is just an ordered list of JobRecord
ids plus the shared `target` they were all queued against. Every file
in a batch is a completely ordinary Job, created and run through the
exact same JobStore/job_queue/rq_tasks pipeline a single-file POST
/api/v1/jobs request already uses -- this module adds nothing new for a
job to go through, only a way to find a group of jobs that were
submitted together and report on them collectively (documents_converter/
api/app.py's POST /api/v1/batch and GET /api/v1/batch/{id}[/download,
/resume]). That reuse is also what "safe concurrency" actually means
here: batch files are enqueued onto the exact same Redis queue every
other conversion already shares (Phase 14, master directive numbering
-- see job_queue.py; originally a shared in-process thread pool before
that phase), so a 50-file batch still only ever runs as many
conversions at once as there are real worker processes consuming that
queue, the same capacity every other caller already competes for --
not a separate, uncapped pool that would let one batch request starve
every other job. Scaling that capacity (running more `worker` replicas)
scales every caller's throughput together, not just one batch's.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .db import SessionLocal
from .models import BatchRecord


@dataclass
class Batch:
    id: str
    created_at: float
    target: str
    job_ids: list[str]
    total: int
    # Phase 17 (master directive numbering): see BatchRecord.user_id's
    # own docstring (models.py).
    user_id: str | None = None

    @classmethod
    def _from_record(cls, record: BatchRecord) -> Batch:
        return cls(
            id=record.id,
            created_at=record.created_at,
            target=record.target,
            job_ids=record.job_ids.split(",") if record.job_ids else [],
            total=record.total,
            user_id=record.user_id,
        )


class BatchStore:
    def __init__(self, retention_seconds: float):
        # Same retention window as JobStore, and for the same reason:
        # by the time a batch row is this old, JobStore's own cleanup
        # has already deleted every job it references, so keeping the
        # batch row around any longer would only ever resolve to
        # "job not found" for each of its ids.
        self.retention_seconds = retention_seconds

    def create(self, target: str, job_ids: list[str], user_id: str | None = None) -> Batch:
        self._cleanup_expired()
        batch_id = uuid.uuid4().hex
        now = time.time()
        with SessionLocal() as session:
            session.add(
                BatchRecord(
                    id=batch_id,
                    created_at=now,
                    target=target,
                    job_ids=",".join(job_ids),
                    total=len(job_ids),
                    user_id=user_id,
                )
            )
            session.commit()
        return Batch(
            id=batch_id, created_at=now, target=target, job_ids=job_ids, total=len(job_ids), user_id=user_id
        )

    def get(self, batch_id: str) -> Batch | None:
        with SessionLocal() as session:
            record = session.get(BatchRecord, batch_id)
            return Batch._from_record(record) if record is not None else None

    def _cleanup_expired(self) -> None:
        # Known, disclosed limitation (Phase 17, master directive
        # numbering): unlike JobStore._cleanup_expired, this doesn't
        # check whether any of a batch's own job_ids were individually
        # `saved` -- a batch row can expire here while a saved job it
        # once grouped still exists and is still directly reachable via
        # GET /api/v1/jobs/{id}. Only the *grouping* (GET
        # /api/v1/batch/{id}) is lost in that case, not the job or its
        # result. Left this way rather than joining against every
        # referenced JobRecord's `saved` flag on every cleanup pass, for
        # a case this project doesn't yet have a real use case
        # justifying that complexity for.
        cutoff = time.time() - self.retention_seconds
        with SessionLocal() as session:
            session.query(BatchRecord).filter(BatchRecord.created_at < cutoff).delete()
            session.commit()
