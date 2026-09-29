#!/usr/bin/env bash
# Real Postgres restore (Phase 20, master directive numbering) -- the
# tested other half of backup_postgres.sh. Restores into the *same*,
# already-running `postgres` service (docker-compose.yml) -- drops and
# recreates the docconv database first (WITH (FORCE), so this works
# even while the api/worker containers still hold open connections to
# it -- confirmed against a real running stack, not assumed), so this
# is a real, destructive replace, not a merge. Confirm you mean it
# before running this against anything you care about.
#
# Usage:
#   ./scripts/restore_postgres.sh path/to/docconv-TIMESTAMP.dump
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 path/to/backup.dump" >&2
    exit 1
fi

BACKUP_FILE="$1"
if [ ! -f "$BACKUP_FILE" ]; then
    echo "No such file: $BACKUP_FILE" >&2
    exit 1
fi

echo "This will DROP and recreate the 'docconv' database, replacing its"
echo "current contents with $BACKUP_FILE. Press Ctrl+C now to abort."
sleep 5

docker compose exec -T postgres psql -U docconv -d postgres -c "DROP DATABASE IF EXISTS docconv WITH (FORCE);"
docker compose exec -T postgres psql -U docconv -d postgres -c "CREATE DATABASE docconv;"
docker compose exec -T postgres pg_restore -U docconv -d docconv < "$BACKUP_FILE"

echo "Restore complete."
