"""
Minimal audit trail for document conversions (Phase 11, docs/PHASE_0_AUDIT.md
numbering continued).

Separate on purpose from the ad-hoc print() debug logging already
scattered through app.py (request lifecycle / error diagnostics, meant
for a developer watching container logs, not a durable record) -- this
is a structured, append-only record of *what happened*, for a service
whose own README describes handling "trusted official documents": who
(by IP -- there's no user-account system yet, see auth.py) converted
what kind of file to what target, when, and whether it succeeded.

Never logs the document's content, its client-supplied filename, or
anything read from inside it -- same policy as every other log line in
this project (docs/PHASE_0_AUDIT.md Risk Register #1). Callers of
log_event() are responsible for only passing safe metadata; this module
doesn't inspect the fields it's given.

Emits one JSON object per line to a dedicated logger
("documents_converter.audit"), always to stdout (the standard
12-factor-app approach -- captured by whatever log aggregation the
deployment already has), and additionally to config.AUDIT_LOG_PATH if
that's set, for a deployment that wants a durable file on a mounted
volume without standing up a database just for this.

Phase 18 (master directive numbering) added a third destination: a
real database row (AuditLogRecord, models.py), so GET
/api/v1/admin/audit-log can actually show this history back through
the API instead of only ever living in stdout/an optional file no
running process can query. Best-effort and never fatal: a DB write
failure here is caught and swallowed (loudly printed, not silently
dropped) rather than ever breaking the real feature the event
describes -- logging that a conversion happened must never be able to
make the conversion itself fail.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from . import config

_logger = logging.getLogger("documents_converter.audit")
_logger.setLevel(logging.INFO)
# Propagation stays at its default (True) deliberately: pytest's caplog
# fixture captures records via a handler on the root logger, not this
# one, so disabling propagation would make every audit event invisible
# to tests (confirmed the hard way -- see tests/test_audit.py). In the
# real running app this project doesn't configure the root logger at
# all, so the explicit handler below remains the only thing that
# actually prints these lines; propagation has nothing to duplicate into.


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # log_event() below always passes a pre-serialized JSON string as
        # the message -- this formatter's only job is to skip logging's
        # usual "LEVEL:name:message" prefix so each line is valid JSON on
        # its own, parseable by any standard log shipper.
        return record.getMessage()


def _configure_handlers() -> None:
    if _logger.handlers:
        return  # module-level singleton, same pattern as app.py's _job_store etc.
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(_JsonLineFormatter())
    _logger.addHandler(stream_handler)
    if config.AUDIT_LOG_PATH:
        file_handler = logging.FileHandler(config.AUDIT_LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(_JsonLineFormatter())
        _logger.addHandler(file_handler)


_configure_handlers()


def log_event(event: str, **fields: Any) -> None:
    """
    Writes one audit record as a single JSON-line log entry (stdout,
    plus config.AUDIT_LOG_PATH if set), and, best-effort, one row to
    the database (Phase 18, master directive numbering -- see module
    docstring).

    :param event: one of "convert_requested", "convert_completed",
        "convert_failed", "job_created", "job_completed", "job_failed".
    :param fields: safe metadata only -- extension, target format, size,
        status, ids, client IP. Never document content, the
        client-supplied filename, or anything read from inside the file.
    """
    ts = time.time()
    record = {"ts": ts, "event": event, **fields}
    _logger.info(json.dumps(record, default=str))
    _persist_event(ts, event, fields)


def _persist_event(ts: float, event: str, fields: dict) -> None:
    # Imported here, not at module level -- avoids paying for a database
    # engine/connection setup (db.py's module-level `engine =
    # _make_engine(...)`) for every single importer of this module,
    # including one that logs events but never otherwise touches the
    # database. Cheap enough per call (Python caches the import) that
    # this isn't a real performance concern for how often log_event()
    # actually runs.
    try:
        from .db import SessionLocal
        from .models import AuditLogRecord

        with SessionLocal() as session:
            session.add(
                AuditLogRecord(ts=ts, event=event, fields_json=json.dumps(fields, default=str))
            )
            session.commit()
    except Exception as e:
        # Never lets a database hiccup break the real feature the event
        # describes -- see module docstring. Printed, not silently
        # dropped, so a genuinely broken audit-log persistence path is
        # still visible to whoever operates this service.
        print(f"[audit] failed to persist event {event!r} to the database: {e!r}")
