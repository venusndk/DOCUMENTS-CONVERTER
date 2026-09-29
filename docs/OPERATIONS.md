# Operations Runbook (Phase 20 completion, master directive numbering: Production Release Engineering)

What someone actually running this service needs, in one place —
deployment, configuration, backups, monitoring, scaling, and disaster
recovery. Every command here has been run for real against a live
`docker compose` stack while writing this document, not just reviewed
for plausibility; where that's not true, it says so.

## Deploying

```bash
git clone https://github.com/venusndk/DOCUMENTS-CONVERTER.git
cd DOCUMENTS-CONVERTER
cp .env.example .env   # fill in what you need -- see below
docker compose up --build -d
curl http://localhost:8000/health
```

`docker-compose.yml` brings up four services: `postgres`, `redis`,
`api`, and `worker` — see that file's own comments for why Postgres
(not the default local SQLite) and a shared `work_data` volume are both
load-bearing here, not incidental choices. `restart: unless-stopped` on
every service and `deploy.resources.limits` on `api`/`worker` (real,
Compose-enforced limits, confirmed via `docker inspect` while writing
this phase — not a Swarm-only no-op) are this phase's own additions;
tune the limits to your real workload, they're generous starting
points, not numbers validated against a specific production traffic
pattern.

Scale worker capacity independently of the API process:

```bash
docker compose up -d --scale worker=3
```

### A pre-built image, instead of building locally

Every push to `main` that passes the full test suite builds and
publishes a real image to GitHub Container Registry (`.github/workflows/test.yml`'s
`docker` job — see **CI/CD** below):

```bash
docker pull ghcr.io/venusndk/documents-converter:latest
# or a specific, immutable commit:
docker pull ghcr.io/venusndk/documents-converter:<commit-sha>
```

Point `docker-compose.yml`'s `api`/`worker` services at `image:
ghcr.io/venusndk/documents-converter:latest` instead of `build: .` to
use this directly rather than building locally.

## Environment configuration

`.env.example` lists every environment variable this project reads,
each with a one-line purpose and a safe example — copy what you need
into a real `.env` (gitignored) or your orchestrator's own secret/config
mechanism. A fresh checkout needs **none** of them set; every default is
this project's own documented local/dev behavior.

The ones that matter most before exposing this beyond a trusted
network, all covered in `.env.example`:

| Variable | Why it matters in production |
|---|---|
| `ENVIRONMENT=production` | Refuses to start at all with `API_KEYS` unset — a real, enforced guard (`app._check_startup_config`), not just a suggestion |
| `API_KEYS` | Without this, every `/api/v1/*` route is open to anyone who can reach it |
| `SESSION_SECRET` | Left unset, every restart invalidates every logged-in browser's CSRF token (sessions themselves still work — only re-login is forced) |
| `DATABASE_URL` | Point this at real Postgres — the default local SQLite file has no place to live once `api`/`worker` are separate containers |
| `ADMIN_EMAILS` | The only way any account ever gets real admin access (Phase 18) |

## Backups

`scripts/backup_postgres.sh` and `scripts/restore_postgres.sh` — both
run for real while writing this phase, not just written and assumed to
work:

```bash
./scripts/backup_postgres.sh              # writes ./backups/docconv-<timestamp>.dump
./scripts/backup_postgres.sh /mnt/backups # or any other directory
```

Real, verified restore cycle (this exact sequence was actually run
against a live stack): created a real account, backed it up, created a
*second* account, then restored from the first backup — the second
account was genuinely gone afterward and the first was correctly back,
confirming this is a real replace, not a merge, and that it actually
round-trips real data, not just an empty schema.

```bash
./scripts/restore_postgres.sh ./backups/docconv-20260101-120000.dump
```

`pg_dump -Fc` (custom format): compressed, and restorable with
`pg_restore` (which the restore script already does) rather than a
plain SQL dump. Restoring uses `DROP DATABASE ... WITH (FORCE)`,
confirmed to work even while `api`/`worker` still hold open
connections to it — no manual "stop the app first" step needed.

**What this does not do**: schedule itself, prune old backups, or ship
them offsite. Wire `backup_postgres.sh` into cron / Task Scheduler /
your orchestrator's own scheduled-job mechanism, and decide a real
retention policy and offsite/durable storage target for your own
deployment — genuinely deployment-specific decisions this project can't
make on your behalf.

`work_data` (the shared job scratch volume) and Redis are deliberately
**not** backed up — see **Disaster recovery** below for exactly why
that's safe.

## Monitoring & health checks

| Endpoint | Purpose |
|---|---|
| `GET /health` | Fast liveness/readiness probe — Tesseract + database only, unauthenticated, safe for a load balancer to poll constantly (Phase 6's own stated reason for leaving it unauthenticated) |
| `GET /metrics` | Real Prometheus text exposition format (Phase 19) — HTTP request counts/latencies, live job-queue depth, job counts by status. Point a real Prometheus server's scrape config at this. |
| `GET /admin` | The real admin dashboard (Phase 18) — queue status, provider status, job analytics, recent audit events. Requires a real admin account (`ADMIN_EMAILS`) to show data. |
| `GET /api/v1/admin/*` | The same data `GET /admin` renders, if you want to build your own dashboard or alerting against it directly |

A reasonable minimal alerting set for a real deployment (not yet wired
up anywhere in this project — a real Prometheus/Alertmanager or
equivalent setup is deployment-specific):

- `GET /health` returning anything but `200` — the API is either down
  or one of its two hard dependencies (Tesseract, the database) is not.
- `documents_converter_job_queue_failed` (from `/metrics`) trending up
  — jobs are failing faster than expected.
- `documents_converter_jobs_current{status="queued"}` growing without
  bound — workers aren't keeping up; scale `worker` (see above).

## Disaster recovery

**What's actually durable, and what isn't:**

| Data | Where it lives | Durable? |
|---|---|---|
| Accounts, job records, batch records, audit log | Postgres | Yes — back it up (see above) |
| Job queue state (what's currently queued/in-flight) | Redis | No, and this is fine — see below |
| A job's input/output files while it's being processed | `work_data` volume | No, and this is fine too — see below |

**Losing Redis** loses only *in-flight* queue state — which jobs are
currently queued or being worked on. It does **not** lose the
`JobRecord` rows describing them (those are in Postgres). A job that
was queued or processing when Redis was lost will show its last-known
status in Postgres forever (there's no code path that notices Redis
itself was wiped and marks orphaned jobs failed) — a real, disclosed
gap: recovering from this today means manually identifying and
re-submitting affected jobs, not an automatic recovery. Worth knowing
before treating Redis as something that needs its own backup strategy;
it doesn't, but this manual step is the real cost of that choice.

**Losing the `work_data` volume** loses whatever input/output files
were sitting in it at the time — jobs mid-processing lose their
input, and `GET /api/v1/jobs/{id}/result` can no longer serve a
completed job's file even though `JobRecord` still says `completed`.
Same shape as the Redis case: no automatic detection or recovery, a
real limitation for a deployment that needs completed results to
survive indefinitely (a completed job's result is meant to be
downloaded promptly, not treated as long-term storage — see
`JOB_RETENTION_SECONDS` in `.env.example`).

**Recovering from a full Postgres loss**: stand up a fresh `postgres`
container/instance, run `./scripts/restore_postgres.sh` against your
most recent backup, then start `api`/`worker` against it — migrations
are idempotent and run automatically at startup
(`db.run_migrations`), so a restored database that's already at the
schema version this code expects needs nothing further.

**What this project has not built**: automated failover, point-in-time
recovery (only whatever your last `pg_dump` captured), or a tested
multi-region deployment. All real, disclosed gaps for a deployment with
stricter RPO/RTO requirements than "restore from the last backup you
took" — genuinely out of scope for what's been built and verified here.

## CI/CD

`.github/workflows/test.yml` runs the full test suite on every push and
PR to `main` (unchanged from earlier phases), then — new this phase — a
`docker` job (`needs: test`, so a broken suite can never reach the
registry) builds the real Docker image on every push and PR too (real,
executed validation that the Dockerfile itself still builds, not just
that Python imports cleanly), and pushes it to
`ghcr.io/venusndk/documents-converter` only on an actual push to `main`
— tagged both `latest` and the exact commit SHA. GitHub Container
Registry, not Docker Hub, needs no new secret at all (the built-in
`GITHUB_TOKEN` already has package-write permission); a fork's PR
build still runs (real validation) but can never push, since it never
has that token's permission.

**Honestly disclosed**: the build-and-validate half of this job has run
for real in this project's own CI (confirmed on this phase's own PR).
The actual registry push has not — that only fires on a real merge to
`main`, which happens after this document is written, not before.

## Incident response basics

- **Service returns 5xx broadly**: check `GET /health` first. If
  `database_available: false`, the database is unreachable — check
  `postgres`'s own container/health status. If Tesseract shows
  unavailable, only OCR-dependent conversions are actually affected;
  everything else keeps working.
- **Jobs stuck at `queued`**: check `GET /api/v1/admin/queue` (needs a
  real admin session) for real worker counts and states — zero workers
  means the `worker` service itself is down or can't reach Redis.
- **A specific account can't be helped through the API**: every
  account-scoped table (`users`, `user_sessions`, `jobs`, `batches`) is
  in Postgres and reachable directly via `psql` for a real, manual
  fix — this project has no separate admin-only data-repair tooling
  beyond the dashboard's own read-only views.
