"""
SQLAlchemy ORM models -- only tables this project has an immediate,
concrete use for. See jobs.py's module docstring for why the job store
was the first thing migrated onto real persistence rather than adding
tables speculatively. BatchRecord (Phase 13, master directive
numbering) is the second, added for the same reason: batch processing
needs its jobs' groupings to survive a restart exactly as much as the
jobs themselves already do.
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class BatchRecord(Base):
    """Phase 13 (master directive numbering): Batch Processing. A batch
    is deliberately thin -- just the ordered list of JobRecord ids it
    fanned out to, plus the shared `target` every file in it was
    converted to. Each file's actual conversion state (queued/
    processing/completed/failed, its error, its result) lives entirely
    in the existing JobRecord row for that file; this table exists only
    so a group of jobs created together can be found and reported on
    together (documents_converter/api/batch.py), not to duplicate any
    state JobRecord already owns."""

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    # Comma-separated job ids (JobRecord.id is a plain hex uuid4 with no
    # comma in it, so this needs no escaping) -- one extra column, not a
    # whole child table, for what's really just an ordered list with no
    # per-row query needs of its own.
    job_ids: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_media_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    result_filename: Mapped[str] = mapped_column(
        String(255), nullable=False, default="converted.xlsx"
    )
    work_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set only for jobs whose capability produced a review artifact
    # (currently just OCR->Excel -- see converters/ocr_to_excel.py) --
    # master directive Phase 7 completion's "preview"/"human review"
    # support. NULL for every other capability's jobs.
    review_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 14 (master directive numbering): the original `target` this
    # job was queued against, needed to re-resolve the same Capability
    # when re-enqueueing a job (POST .../retry, POST .../resume) without
    # requiring the caller to re-upload the file or re-specify `target`
    # -- everything needed to redo the conversion is already sitting in
    # work_dir plus this column. Nullable only because it didn't exist
    # before this phase; every job created from here on sets it.
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
