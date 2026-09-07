"""
SQLAlchemy ORM models -- currently just the one table this project
actually has an immediate, concrete use for. See jobs.py's module
docstring for why the job store is the first (and, for now, only) thing
migrated onto real persistence rather than adding tables speculatively.
"""

from __future__ import annotations

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


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
