#!/usr/bin/env bash
#
# One-shot installer for the nginx-stack deployment.
# Run on the droplet with: sudo bash /var/www/budget/deploy/install.sh
#
# Idempotent: safe to re-run after a code update. Steps that have already
# happened (apt packages, directories, venv) are skipped.
#
# Does NOT touch your nginx config — you'll do that manually so you can
# eyeball the diff first. See DEPLOY_NGINX.md §5.

set -euo pipefail

APP_DIR="/var/www/budget"
DATA_DIR="/var/lib/budget"
LOG_DIR="/var/log/budget"
BACKUP_DIR="/var/backups/budget"

# --- sanity ---
if [ "$EUID" -ne 0 ]; then
    echo "Run as root (sudo bash install.sh)." >&2
    exit 1
fi
if [ ! -d "$APP_DIR" ]; then
    echo "Expected the code at $APP_DIR — adjust APP_DIR at the top of this script." >&2
    exit 1
fi

echo ">>> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip sqlite3

echo ">>> Creating data, log, backup directories"
mkdir -p "$DATA_DIR" "$LOG_DIR" "$BACKUP_DIR"
chown www-data:www-data "$DATA_DIR" "$LOG_DIR" "$BACKUP_DIR"

echo ">>> Building venv at $APP_DIR/.venv"
cd "$APP_DIR"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo ">>> Setting ownership on $APP_DIR"
chown -R www-data:www-data "$APP_DIR"

echo ">>> Preparing .env"
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
if ! grep -q '^SECRET_KEY=[0-9a-f]\{32,\}' "$APP_DIR/.env"; then
    NEW_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    sed -i "/^SECRET_KEY=/d" "$APP_DIR/.env"
    echo "SECRET_KEY=$NEW_SECRET" >> "$APP_DIR/.env"
    echo "    -> generated a new SECRET_KEY"
fi
chown www-data:www-data "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo ">>> Installing systemd unit"
cp "$APP_DIR/deploy/budget.service" /etc/systemd/system/budget.service
systemctl daemon-reload
systemctl enable budget.service
systemctl restart budget.service

echo ">>> Waiting for the app to come up"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS http://127.0.0.1:8000/healthz > /dev/null 2>&1; then
        echo "    -> healthy"
        break
    fi
    sleep 1
done

echo
echo "Done. The app is running at http://127.0.0.1:8000."
echo "Next steps:"
echo "  1. Configure nginx to proxy your domain to 127.0.0.1:8000 (see deploy/budget.nginx)"
echo "  2. Visit your domain to complete first-run setup (create your admin account)"
echo "  3. Add the nightly backup cron entry (see scripts/backup.sh)"
