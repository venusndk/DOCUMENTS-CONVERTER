#!/usr/bin/env bash
# Real Postgres backup (Phase 20, master directive numbering: Production
# Release Engineering) -- pg_dump in custom format (-Fc: compressed,
# supports selective/parallel restore via pg_restore, unlike a plain
# SQL dump), run through the same `postgres` service docker-compose.yml
# already defines rather than needing a separate pg_dump install on the
# host. Paired with restore_postgres.sh -- see that script's own header
# for the tested restore half of this; both were run for real against a
# live docker compose stack while writing this phase, not just reviewed.
#
# Usage:
#   ./scripts/backup_postgres.sh [output-directory]
#
# Defaults to ./backups/ (gitignored -- see .gitignore) if no directory
# is given. Each backup is one timestamped file
# (docconv-YYYYmmdd-HHMMSS.dump). This script does not delete old
# backups itself -- retention policy is a real, deployment-specific
# decision (see docs/OPERATIONS.md), not one this script makes for you.
set -euo pipefail

OUTPUT_DIR="${1:-./backups}"
mkdir -p "$OUTPUT_DIR"

TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
OUTPUT_FILE="$OUTPUT_DIR/docconv-$TIMESTAMP.dump"

echo "Backing up the real 'docconv' database via the running postgres container..."
docker compose exec -T postgres pg_dump -U docconv -Fc docconv > "$OUTPUT_FILE"

SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)
echo "Backup written: $OUTPUT_FILE ($SIZE)"
