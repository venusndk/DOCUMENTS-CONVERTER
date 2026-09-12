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

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    """Phase 17 (master directive numbering): Authentication & User
    Workspace. Deliberately thin -- accounts.py owns every real
    behavior (password hashing/verification, session issuance); this is
    just the row. `preferences_json` is one column, not a child table,
    for the same reason BatchRecord.job_ids is one column: an
    unstructured, caller-defined bag of settings (default target
    format, UI theme, ...) has no query needs of its own -- it's read
    and written whole, never filtered on."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Stored lowercased (accounts.py normalizes before every read/write)
    # so "a@b.com" and "A@B.com" are the same account -- unique still
    # only actually holds if every caller normalizes the same way.
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    preferences_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class UserSession(Base):
    """A real, server-side session row -- not a JWT, per this phase's
    own explicit choice (see config.SESSION_SECRET's docstring). Only
    `token_hash` (SHA-256 of the raw cookie value) is ever stored, the
    same reasoning as never storing a plaintext password: a database
    read (backup exposure, a logged query, an injection bug) shouldn't
    by itself hand over a usable session, the same way it shouldn't
    hand over a usable password."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_seen_at: Mapped[float] = mapped_column(Float, nullable=False)


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
    # Phase 17 (master directive numbering): which logged-in user
    # submitted this batch, if any -- nullable, since an unauthenticated
    # or API-key-only caller (this project's existing, still-fully-
    # supported usage) has no account to attach it to. See
    # JobRecord.user_id's own docstring for the full reasoning.
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


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
    # Phase 17 (master directive numbering): which logged-in user
    # submitted this job, if any -- nullable, so an unauthenticated or
    # API-key-only caller (this project's existing, still-fully-
    # supported usage) keeps working exactly as before, with no account
    # attached. Set from an *optional* session-cookie check
    # (accounts.get_current_user_optional), never required, at
    # POST /api/v1/jobs and /api/v1/batch.
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # "Saved jobs": an explicit per-job opt-in (POST .../save), not "every
    # logged-in user's job is kept forever" -- JobStore._cleanup_expired
    # exempts a saved job from its normal retention-window deletion, but
    # only this one job, not every job a given user has ever submitted.
    # Keeps storage growth bounded and caller-controlled rather than an
    # unbounded, automatic liability of merely being logged in. Anonymous
    # jobs (user_id is None) can't be saved -- there's no account to list
    # them under later, so saving one would be a no-op nobody could ever see.
    saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
