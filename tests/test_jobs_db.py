"""
Tests for the database-backed job store (documents_converter/api/jobs.py,
db.py, models.py) and the Alembic migration that creates its table --
Phase 1 completion, master directive numbering.

Most tests here exercise JobStore directly against the shared test
session's database (an isolated temp SQLite file -- see conftest.py's
DATABASE_URL setup and _migrated_test_database fixture), since that's
already real persistence, not a mock. The one exception is the
from-scratch migration test below, which deliberately runs
`alembic upgrade head` as a real subprocess against a brand new,
never-touched file, to prove the migration works from nothing -- not
just against a database this test session already migrated.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
import time
from pathlib import Path

from documents_converter.api.jobs import Job, JobStore

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_create_returns_a_queued_job_with_an_id():
    store = JobStore(retention_seconds=3600)
    job = store.create()
    assert job.status == "queued"
    assert job.id
    assert isinstance(job.created_at, float)


def test_get_returns_none_for_unknown_id():
    store = JobStore(retention_seconds=3600)
    assert store.get("does-not-exist") is None


def test_create_then_get_round_trips():
    store = JobStore(retention_seconds=3600)
    created = store.create()
    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.status == "queued"


def test_update_persists_across_a_fresh_get():
    store = JobStore(retention_seconds=3600)
    job = store.create()
    store.update(
        job.id,
        status="completed",
        result_path=Path("/tmp/whatever/output.xlsx"),
        error=None,
    )
    fetched = store.get(job.id)
    assert fetched.status == "completed"
    # Path objects round-trip through the DB's text column and back.
    assert fetched.result_path == Path("/tmp/whatever/output.xlsx")


def test_update_on_unknown_id_does_not_raise():
    store = JobStore(retention_seconds=3600)
    store.update("does-not-exist", status="completed")  # must not raise


def test_cleanup_removes_expired_completed_jobs_and_their_work_dir(tmp_path):
    store = JobStore(retention_seconds=1)
    job = store.create()
    work_dir = tmp_path / f"job-{job.id}"
    work_dir.mkdir()
    (work_dir / "output.xlsx").write_bytes(b"fake")

    store.update(job.id, status="completed", work_dir=work_dir)
    # Backdate created_at past the 1-second retention window directly --
    # faster and more deterministic than a real time.sleep(1) in a test.
    from documents_converter.api.db import SessionLocal
    from documents_converter.api.models import JobRecord

    with SessionLocal() as session:
        record = session.get(JobRecord, job.id)
        record.created_at = time.time() - 10
        session.commit()

    store2 = JobStore(retention_seconds=1)
    store2.create()  # cleanup runs as a side effect of create()

    assert store.get(job.id) is None
    assert not work_dir.exists()


def test_cleanup_leaves_queued_and_processing_jobs_alone():
    store = JobStore(retention_seconds=0)  # everything old enough to expire
    queued = store.create()
    processing = store.create()
    store.update(processing.id, status="processing")

    store.create()  # triggers cleanup as a side effect

    assert store.get(queued.id) is not None
    assert store.get(processing.id) is not None


def test_alembic_upgrade_creates_jobs_table_from_a_fresh_database(tmp_path):
    """
    Runs the real migration as a real subprocess against a database file
    that has never existed before -- proving the migration itself works
    from nothing, not just that this test session's already-migrated
    database happens to have the table.
    """
    db_path = tmp_path / "fresh.db"
    assert not db_path.exists()

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert db_path.exists()

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "jobs" in tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert {"id", "status", "created_at", "result_media_type", "result_filename"} <= columns
    finally:
        conn.close()
