#!/usr/bin/env bash
# One-time setup of the Bagworm Census app on a fresh Ubuntu 22.04 (jammy) VM,
# with the code coming from a (private) git repository.
#
# Before running, copy the files that are NOT in git to a folder on the VM
# (default: the current folder):  census.db  gcal_credentials.json  gcal_token.json
#
# Usage (on the VM):
#   sudo bash setup_vm.sh  REPO_URL  DOMAIN  EMAIL  [DATA_DIR]
#   e.g.
#   sudo bash setup_vm.sh git@github.com:yourname/bagworm_app.git 34-101-12-34.sslip.io you@mail.example ~/bagworm_data
#
# Layout:
#   /srv/bagworm/app        git checkout + census.db (writable by 'bagworm')
#   /srv/bagworm/venv       Python virtualenv
#   /srv/bagworm/uploads    browser uploads (auto-deleted after UPLOAD_RETENTION_DAYS)
#   /srv/bagworm/.ssh       read-only deploy key for git pull
#   /etc/bagworm/env        secrets / settings (chmod 600)
#
# Later updates: sudo bash /srv/bagworm/app/deploy/update.sh
set -euo pipefail

REPO="${1:?git repository URL (SSH form, e.g. git@github.com:user/bagworm_app.git)}"
DOMAIN="${2:?domain name pointing to this VM (e.g. 34-101-12-34.sslip.io)}"
EMAIL="${3:?email for the HTTPS certificate}"
DATA_DIR="$(cd "${4:-.}" && pwd)"

APP_DIR=/srv/bagworm/app
VENV=/srv/bagworm/venv
KEY=/srv/bagworm/.ssh/id_ed25519
as_app() { sudo -u bagworm -H "$@"; }

echo "== 1/8 System packages"
apt-get update
apt-get install -y git python3-venv python3-dev build-essential \
    libgl1 libglib2.0-0 sqlite3 nginx certbot python3-certbot-nginx

echo "== 2/8 Service user and folders"
id bagworm &>/dev/null || useradd --system --home /srv/bagworm --shell /usr/sbin/nologin bagworm
mkdir -p /srv/bagworm/uploads /srv/bagworm/backups /srv/bagworm/.ssh /etc/bagworm
chown -R bagworm:bagworm /srv/bagworm
chmod 700 /srv/bagworm/.ssh

echo "== 3/8 Get the code from git"
if [[ "$REPO" == git@* || "$REPO" == ssh://* ]]; then
    if [ ! -f "$KEY" ]; then
        as_app ssh-keygen -t ed25519 -N "" -C "bagworm-vm" -f "$KEY" >/dev/null
    fi
    HOST=$(echo "$REPO" | sed -E 's#^(ssh://)?([^@]+@)?([^:/]+).*#\3#')
    ssh-keyscan -t ed25519,rsa "$HOST" 2>/dev/null >> /srv/bagworm/.ssh/known_hosts
    sort -u -o /srv/bagworm/.ssh/known_hosts /srv/bagworm/.ssh/known_hosts
    chown bagworm:bagworm /srv/bagworm/.ssh/known_hosts
    echo
    echo ">> Add this public key to the repository as a READ-ONLY deploy key:"
    echo "   GitHub: repo > Settings > Deploy keys > Add deploy key (leave 'Allow write access' OFF)"
    echo
    cat "$KEY.pub"
    echo
    read -r -p ">> Press Enter once the key has been added... " _
fi

if [ -d "$APP_DIR/.git" ]; then
    as_app git -C "$APP_DIR" pull --ff-only
else
    if [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR")" ]; then
        echo "!! $APP_DIR exists and is not a git checkout - move it away first." >&2
        exit 1
    fi
    as_app git clone "$REPO" "$APP_DIR"
fi

echo "== 4/8 Data and credentials that are not in git (never overwrites)"
for f in census.db gcal_credentials.json gcal_token.json; do
    if [ -f "$APP_DIR/$f" ]; then
        echo "   $f already present - kept"
    elif [ -f "$DATA_DIR/$f" ]; then
        install -o bagworm -g bagworm -m 600 "$DATA_DIR/$f" "$APP_DIR/$f"
        echo "   $f copied from $DATA_DIR"
    else
        echo "   WARNING: $f not found in $DATA_DIR"
    fi
done

echo "== 5/8 Python environment (CPU-only PyTorch)"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install torch torchvision --index-url https://download.pytorch.org/whl/cpu
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R bagworm:bagworm "$VENV"

echo "== 6/8 Settings file"
if [ ! -f /etc/bagworm/env ]; then
    cp "$APP_DIR/deploy/env.example" /etc/bagworm/env
    NEW_SECRET=$(openssl rand -hex 32)
    NEW_ADMIN_PW=$(openssl rand -hex 8)
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${NEW_SECRET}|" /etc/bagworm/env
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${NEW_ADMIN_PW}|" /etc/bagworm/env
    echo ">> Generated /etc/bagworm/env. Admin password:"
    grep ^ADMIN_PASSWORD /etc/bagworm/env
fi
chmod 600 /etc/bagworm/env

echo "== 7/8 systemd service + nginx + HTTPS"
cp "$APP_DIR/deploy/bagworm.service" /etc/systemd/system/bagworm.service
systemctl daemon-reload
systemctl enable --now bagworm
sleep 5
systemctl --no-pager status bagworm | head -n 5

sed "s|__DOMAIN__|$DOMAIN|g" "$APP_DIR/deploy/nginx-bagworm.conf" > /etc/nginx/sites-available/bagworm
ln -sf /etc/nginx/sites-available/bagworm /etc/nginx/sites-enabled/bagworm
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect

echo "== 8/8 Daily database backup (02:00)"
install -m 755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/bagworm-backup
echo "0 2 * * * root /usr/local/bin/bagworm-backup >> /var/log/bagworm-backup.log 2>&1" > /etc/cron.d/bagworm-backup

echo
echo "Done. Open https://$DOMAIN"
echo "Update:  sudo bash $APP_DIR/deploy/update.sh"
echo "Logs:    journalctl -u bagworm -f"
