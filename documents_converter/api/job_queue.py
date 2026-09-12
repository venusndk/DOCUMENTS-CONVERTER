"""
Redis connection and RQ queue (Phase 14, master directive numbering:
Job Queue & Real-Time Processing). Replaces the in-process
ThreadPoolExecutor as the transport for /api/v1/jobs and /api/v1/batch
-- a separate worker process (worker_main.py) pulls jobs off this queue
instead of the API process running conversions on its own threads.

Deliberately just a connection and a Queue object, not a bigger
abstraction: RQ's own `Queue`/`Job`/`Retry` types are already the right
level of abstraction for what this project needs (see rq_tasks.py and
app.py's batch/job endpoints for how they're used), so wrapping them
further would only be indirection with nothing behind it.

Constructing `redis.Redis.from_url(...)` doesn't connect eagerly --
redis-py connects lazily, on the first real command -- so importing
this module (which every request handler does, via app.py) never fails
just because Redis happens to be unreachable at import time. The
failure that matters (Redis actually unreachable) surfaces at the one
call site that needs a live connection: `queue.enqueue(...)`, inside
app.py's create_job/create_batch, both of which already handle it as a
503 rather than an unhandled exception -- see their
_enqueue_conversion_job docstring.

`get_redis_connection()` returns one shared, lazily-created client for
the whole process, not a fresh one per call -- confirmed the wrong way
first: an earlier version built a brand-new `redis.Redis` (a brand-new
connection pool, not a shared one -- a fresh client object never reuses
another instance's pool no matter how good redis-py's own pooling is)
on every single call, and the one caller that calls this in a tight
loop -- app.py's GET .../events, polling roughly every 0.5s for up to
several minutes per open connection -- could rack up hundreds of
never-explicitly-closed connections over one long-lived SSE request.
Implicated (not conclusively proven, but never reproduced again after
fixing it) in a real CI failure: a background test worker (tests/
conftest.py) that stopped processing every job for the rest of a
session partway through a real run, right as a handful of long-lived
SSE tests would have been accumulating exactly this kind of connection
pressure. redis.Redis instances are explicitly documented as
thread-safe and meant to be shared -- this is the intended usage, not
a workaround.
"""

from __future__ import annotations

import redis
from rq import Queue

from . import config

#: Name of the one queue this project uses. A second, differently
#: prioritized queue (e.g. a "priority" lane) is a reasonable later
#: addition once there's an actual need for one -- not built
#: speculatively now, same standing policy as storage.py's single
#: backend and models.py's two tables.
QUEUE_NAME = "conversions"

_connection: redis.Redis | None = None


def get_redis_connection() -> redis.Redis:
    global _connection
    if _connection is None:
        # protocol=2 (RESP2): redis-py defaults to negotiating RESP3 via
        # a HELLO command on connect, which only Redis 6+ understands.
        # Found the hard way against this project's own local dev
        # machine: its native Windows Redis (a years-old, unofficial
        # port -- version 3.0.504) has no HELLO command at all, so
        # every connection failed outright with "unknown command
        # 'HELLO'" -- not a timeout, not a slow-to-notice degradation,
        # a hard failure on the very first command. RESP2 has been
        # supported by every Redis version this project could plausibly
        # run against, old or new (including the redis:7-alpine used in
        # Docker/Compose/CI), so this is strictly more compatible, not
        # a downgrade for anyone already on a modern server.
        # socket_connect_timeout=2: a real Redis should answer within a
        # couple seconds; this is also what lets
        # tests/conftest.py's @requires_redis check fail fast against a
        # genuinely absent Redis instead of hanging on the OS's own,
        # much longer TCP connect timeout during test collection.
        _connection = redis.Redis.from_url(
            config.REDIS_URL, protocol=2, socket_connect_timeout=2
        )
    return _connection


def get_queue() -> Queue:
    """A fresh Queue object on every call (constructing one does no
    I/O and holds no resources of its own -- it's just a thin wrapper
    around the queue name and the shared connection above), bound to
    the one shared connection rather than a new one each time."""
    return Queue(QUEUE_NAME, connection=get_redis_connection())
