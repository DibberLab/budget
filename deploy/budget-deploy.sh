#!/usr/bin/env bash
#
# Deploy the latest main to this droplet. GitHub Actions runs this over SSH
# (its key is locked to this script in /root/.ssh/authorized_keys), and you
# can also run it by hand as root.
#
# Install / update on the droplet:
#   install -m 755 /var/www/budget/deploy/budget-deploy.sh /usr/local/bin/budget-deploy
#
# Steps: pull main, reinstall packages if requirements.txt changed, restart
# budget, check /healthz. Exits non-zero (failing the GitHub run) on any error.

set -euo pipefail

APP_DIR="/var/www/budget"
BRANCH="main"
HEALTH_URL="http://127.0.0.1:8000/healthz"

# Only one deploy at a time.
exec 9>/run/budget-deploy.lock
flock -n 9 || { echo "Another deploy is already running." >&2; exit 1; }

cd "$APP_DIR"
git_() { git -c safe.directory="$APP_DIR" "$@"; }

OLD_REV="$(git_ rev-parse HEAD)"
echo ">>> Pulling $BRANCH (currently ${OLD_REV:0:7})"
git_ fetch --quiet origin "$BRANCH"
git_ merge --ff-only "origin/$BRANCH"
NEW_REV="$(git_ rev-parse HEAD)"
echo "    -> now at ${NEW_REV:0:7}"

if [ "$OLD_REV" != "$NEW_REV" ] && git_ diff --name-only "$OLD_REV" "$NEW_REV" | grep -qx 'requirements.txt'; then
    echo ">>> requirements.txt changed, reinstalling packages"
    .venv/bin/pip install --quiet -r requirements.txt
fi

chown -R www-data:www-data "$APP_DIR"

echo ">>> Restarting budget"
systemctl restart budget.service

echo ">>> Checking it came back up"
for _ in $(seq 1 20); do
    if curl -fsS "$HEALTH_URL" > /dev/null 2>&1; then
        echo "    -> healthy at ${NEW_REV:0:7}"
        exit 0
    fi
    sleep 1
done

echo "!!! budget did not pass $HEALTH_URL within 20s. Recent logs:" >&2
systemctl status budget.service --no-pager -l >&2 || true
journalctl -u budget.service -n 50 --no-pager >&2 || true
exit 1
