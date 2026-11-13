#!/usr/bin/env bash
# Pull the latest code on the VM and restart the app.
#
#   sudo bash /srv/bagworm/app/deploy/update.sh          # refuses while a census is running/queued
#   sudo bash /srv/bagworm/app/deploy/update.sh --force  # restart anyway (running census -> error)
#
# census.db, gcal_*.json, uploads and /etc/bagworm/env are not in git, so they are untouched.
set -euo pipefail

APP_DIR=/srv/bagworm/app
VENV=/srv/bagworm/venv
FORCE="${1:-}"
as_app() { sudo -u bagworm -H "$@"; }

busy=$(sqlite3 "$APP_DIR/census.db" \
    "SELECT COUNT(*) FROM census_jobs WHERE status IN ('running','queued');" 2>/dev/null || echo 0)
if [ "$busy" != "0" ] && [ "$FORCE" != "--force" ]; then
    echo "!! $busy census job(s) running or queued. Try again later, or use --force." >&2
    exit 1
fi

OLD=$(as_app git -C "$APP_DIR" rev-parse HEAD)
as_app git -C "$APP_DIR" pull --ff-only
NEW=$(as_app git -C "$APP_DIR" rev-parse HEAD)

if [ "$OLD" = "$NEW" ]; then
    echo "Already up to date ($NEW). Nothing to do."
    exit 0
fi

echo "== Changes:"
as_app git -C "$APP_DIR" log --oneline "$OLD..$NEW"

if as_app git -C "$APP_DIR" diff --quiet "$OLD" "$NEW" -- requirements.txt; then
    echo "== requirements.txt unchanged - skipping pip"
else
    echo "== requirements.txt changed - installing"
    "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" \
        --extra-index-url https://download.pytorch.org/whl/cpu
    chown -R bagworm:bagworm "$VENV"
fi

if ! as_app git -C "$APP_DIR" diff --quiet "$OLD" "$NEW" -- deploy/bagworm.service; then
    cp "$APP_DIR/deploy/bagworm.service" /etc/systemd/system/bagworm.service
    systemctl daemon-reload
fi

systemctl restart bagworm
sleep 5
systemctl --no-pager status bagworm | head -n 5
echo "Updated $OLD -> $NEW"
echo "Rollback: sudo -u bagworm git -C $APP_DIR reset --hard $OLD && sudo systemctl restart bagworm"
