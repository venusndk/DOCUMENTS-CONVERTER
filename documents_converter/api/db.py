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

_REPO_ROOT = Path(__file__).resolve().parents[2]


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


def run_migrations() -> None:
    """
    Applies any pending Alembic migrations against config.DATABASE_URL
    at startup, so a fresh checkout (or a fresh container against a
    fresh database) doesn't need a separate manual `alembic upgrade
    head` step to become usable -- consistent with this project's
    running "zero extra setup" bar for local/dev use.

    Idempotent (upgrading an already-current database is a no-op),
    which matters here since this can run every time a process starts,
    not just once. As of Phase 14 (master directive numbering), two
    independent processes call this at their own startup -- app.py's
    FastAPI lifespan handler and worker_main.py's RQ worker entry point
    -- specifically so docker-compose.yml's `api` and `worker`
    containers have no startup-ordering dependency on each other for
    this: whichever reaches the database first performs the migration,
    the other's call is a no-op. Lives here, not in app.py (where it
    originated before Phase 14), so worker_main.py can call it without
    importing app.py and constructing the whole FastAPI app just for
    this one function -- a worker process has no reason to do that.

    For a deployment that runs multiple replicas against the same
    database, running migrations as an explicit separate release step
    instead (skip calling this, run `alembic upgrade head` once before
    rolling out) avoids every replica racing to migrate on boot --
    tracked as a known simplification for this single-instance-shaped
    project, not silently assumed away.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
