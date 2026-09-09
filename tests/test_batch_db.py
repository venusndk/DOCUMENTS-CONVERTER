"""
Tests for the database-backed batch store (documents_converter/api/
batch.py, models.py) and the Alembic migration that creates its table
-- Phase 13, master directive numbering (Batch Processing).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from documents_converter.api.batch import BatchStore

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_create_returns_a_batch_with_an_id_and_the_given_job_ids():
    store = BatchStore(retention_seconds=3600)
    batch = store.create("xlsx", ["job-a", "job-b"])
    assert batch.id
    assert batch.target == "xlsx"
    assert batch.job_ids == ["job-a", "job-b"]
    assert batch.total == 2


def test_get_returns_none_for_unknown_id():
    store = BatchStore(retention_seconds=3600)
    assert store.get("does-not-exist") is None


def test_create_then_get_round_trips_job_order():
    store = BatchStore(retention_seconds=3600)
    created = store.create("images", ["j1", "j2", "j3"])
    fetched = store.get(created.id)
    assert fetched.job_ids == ["j1", "j2", "j3"]
    assert fetched.target == "images"
    assert fetched.total == 3


def test_create_with_no_jobs_round_trips_an_empty_list():
    """A batch where every file failed validation before any job was
    queueable is still a real, reportable batch -- not an error."""
    store = BatchStore(retention_seconds=3600)
    created = store.create("xlsx", [])
    fetched = store.get(created.id)
    assert fetched.job_ids == []
    assert fetched.total == 0


def test_cleanup_removes_expired_batches():
    store = BatchStore(retention_seconds=1)
    batch = store.create("xlsx", ["j1"])

    from documents_converter.api.db import SessionLocal
    from documents_converter.api.models import BatchRecord
    import time

    with SessionLocal() as session:
        record = session.get(BatchRecord, batch.id)
        record.created_at = time.time() - 10
        session.commit()

    store2 = BatchStore(retention_seconds=1)
    store2.create("xlsx", ["j2"])  # cleanup runs as a side effect of create()

    assert store.get(batch.id) is None


def test_cleanup_leaves_recent_batches_alone():
    store = BatchStore(retention_seconds=3600)
    batch = store.create("xlsx", ["j1"])
    store.create("xlsx", ["j2"])  # triggers cleanup as a side effect
    assert store.get(batch.id) is not None


def test_alembic_upgrade_creates_batches_table_from_a_fresh_database(tmp_path):
    """Same proof as test_jobs_db.py's equivalent test: runs the real
    migration as a real subprocess against a database file that has
    never existed before."""
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
        assert "batches" in tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
        assert {"id", "created_at", "target", "job_ids", "total"} <= columns
    finally:
        conn.close()
