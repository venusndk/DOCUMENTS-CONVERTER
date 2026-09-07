"""
SQLAlchemy engine/session setup -- the persistence layer master directive
Phase 1 ("Production Foundation") asks for.

Supports both backends config.DATABASE_URL can point to, deliberately:
a local SQLite file by default (zero extra infrastructure -- a fresh
checkout keeps working the way every other default in this project
does) and a real Postgres instance for a deployment that needs jobs to
survive a restart and be visible across more than one replica. Both are
exercised by tests/test_jobs_db.py, not just assumed to work because
SQLAlchemy supports both dialects in principle.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def _make_engine(database_url: str):
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        # A local file path (not ":memory:") needs its parent directory to
        # exist before sqlite3 will create the file -- matters for the
        # zero-config default (./data/documents_converter.db), which has
        # no reason to exist yet on a fresh checkout or a fresh container.
        db_path = database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        database_url,
        # SQLite connections are bound to the thread that created them by
        # default; this app's job store is read/written from multiple
        # ThreadPoolExecutor worker threads (see api/app.py), so that
        # default would raise on every cross-thread access. Postgres has
        # no such restriction -- this flag is a no-op there.
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )

    if is_sqlite:
        # WAL mode lets one writer and multiple readers proceed
        # concurrently instead of serializing on a single file lock --
        # meaningfully reduces "database is locked" errors under this
        # app's actual access pattern (a background job-conversion thread
        # updating status while a request thread polls it).
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine = _make_engine(config.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def check_db_connection() -> bool:
    """Used by GET /health -- an actual round trip, not just 'the engine
    object was constructed without raising'."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
