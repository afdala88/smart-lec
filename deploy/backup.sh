#!/usr/bin/env bash
# Daily consistent backup of census.db (safe while the app is running).
# Keeps 14 days locally; also copies to Cloud Storage if BACKUP_BUCKET is set
# in /etc/bagworm/env (e.g. BACKUP_BUCKET=gs://bagworm-backup).
set -euo pipefail
set -a; . /etc/bagworm/env; set +a

DB=/srv/bagworm/app/census.db
OUT=/srv/bagworm/backups/census_$(date +%Y%m%d_%H%M).db

sqlite3 "$DB" ".backup '$OUT'"
gzip "$OUT"
find /srv/bagworm/backups -name 'census_*.db.gz' -mtime +14 -delete

if [ -n "${BACKUP_BUCKET:-}" ]; then
    gcloud storage cp "$OUT.gz" "$BACKUP_BUCKET/"
fi
echo "$(date '+%F %T') backup ok: $OUT.gz"
