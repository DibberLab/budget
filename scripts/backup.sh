#!/usr/bin/env bash
#
# Nightly backup script for the household budget SQLite database.
#
# Uses `sqlite3 .backup` which takes a consistent online snapshot — safe
# to run while gunicorn is serving requests. A plain `cp` could corrupt the
# file if SQLite is mid-checkpoint.
#
# Default paths assume the systemd / nginx deployment:
#   DB_PATH      = /var/lib/budget/budget.db
#   BACKUP_DIR   = /var/backups/budget
#   KEEP_DAYS    = 30
#
# Install as a daily cron job (root crontab):
#   0 3 * * *  /var/www/budget/scripts/backup.sh >> /var/log/budget-backup.log 2>&1

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/budget/budget.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/budget}"
KEEP_DAYS="${KEEP_DAYS:-30}"
DATE="$(date +%F)"
DEST="${BACKUP_DIR}/${DATE}.db.gz"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "[$(date)] ERROR: source DB not found at $DB_PATH" >&2
    exit 1
fi

# .backup uses SQLite's online backup API (proper locking, no torn writes).
TMP_SNAP="$(mktemp --tmpdir budget-snap.XXXXXX.db)"
trap 'rm -f "$TMP_SNAP"' EXIT

sqlite3 "$DB_PATH" ".backup '$TMP_SNAP'"

# Verify the snapshot integrity before we commit to keeping it.
if ! sqlite3 "$TMP_SNAP" "PRAGMA integrity_check;" | grep -q "^ok$"; then
    echo "[$(date)] ERROR: snapshot failed integrity_check" >&2
    exit 1
fi

gzip -9 < "$TMP_SNAP" > "$DEST"
chmod 600 "$DEST"

# Prune snapshots older than KEEP_DAYS.
find "$BACKUP_DIR" -name "*.db.gz" -type f -mtime "+${KEEP_DAYS}" -delete

SIZE=$(du -h "$DEST" | cut -f1)
COUNT=$(find "$BACKUP_DIR" -name "*.db.gz" -type f | wc -l | tr -d ' ')
echo "[$(date)] OK: wrote ${DEST} (${SIZE}); ${COUNT} snapshots retained"
