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

Neither `get_redis_connection()` nor `get_queue()` connects eagerly --
redis-py and RQ both connect lazily, on the first real command -- so
importing this module (which every request handler does, via app.py)
never fails just because Redis happens to be unreachable at import
time. The failure that matters (Redis actually unreachable) surfaces at
the one call site that needs a live connection: `queue.enqueue(...)`,
inside app.py's create_job/create_batch, both of which already handle
it as a 503 rather than an unhandled exception -- see their
_enqueue_conversion_job docstring.
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


def get_redis_connection() -> redis.Redis:
    return redis.Redis.from_url(config.REDIS_URL)


def get_queue() -> Queue:
    """A fresh Queue bound to a fresh connection on every call, rather
    than one shared module-level instance -- cheap (redis-py connections
    are pooled internally per client instance, and constructing a Queue
    object does no I/O), and it sidesteps any question of whether a
    long-lived Queue's connection survives a Redis restart across this
    process's lifetime."""
    return Queue(QUEUE_NAME, connection=get_redis_connection())
