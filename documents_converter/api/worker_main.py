"""
Entry point for a real RQ worker process (Phase 14, master directive
numbering: Job Queue & Real-Time Processing) -- run as its own process
(`python -m documents_converter.api.worker_main`), and as its own
container in docker-compose.yml's `worker` service, built from the same
image as the API but with this as its command instead of uvicorn.

Runs its own migration check at startup, same call the API's lifespan
handler makes (db.run_migrations) -- idempotent, so running it from two
processes on startup is safe, and it removes any dependency on startup
*ordering* between the api and worker containers: whichever of the two
actually reaches the database first performs the migration, and the
other's call is a no-op. Without this, a worker that started faster
than the api container (docker-compose's `depends_on` only waits for
the container to be *running*, not for the api's own lifespan/migration
to have finished) could try to read/write a jobs table that doesn't
exist yet. Imports db.run_migrations directly, not through app.py --
this process has no reason to construct the whole FastAPI app just for
one function.
"""

from __future__ import annotations

from rq import Worker

from . import config
from .db import run_migrations
from .job_queue import QUEUE_NAME, get_queue, get_redis_connection


def main() -> None:
    run_migrations()
    connection = get_redis_connection()
    connection.ping()
    print(f"[worker] connected to {config.REDIS_URL}, listening on queue '{QUEUE_NAME}'")
    worker = Worker([get_queue()], connection=connection)
    # with_scheduler=True is not optional: RQ's own automatic Retry
    # (rq_tasks.py, app.py's _enqueue_conversion_job) schedules a
    # delayed retry rather than re-queueing it immediately whenever its
    # `interval` is nonzero -- confirmed the hard way, RQ's own source
    # (rq.worker.base._start_scheduler is only ever invoked when this
    # is True). Without it, a retried job sits in RQ's "scheduled"
    # state forever: nothing ever promotes it back to the real queue,
    # and this project's own JobRecord for it (optimistically marked
    # "queued" the moment the failure was classified as retryable --
    # see rq_tasks.run_conversion_job) would stay wrong right alongside
    # it.
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
