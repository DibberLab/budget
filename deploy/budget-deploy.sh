#!/usr/bin/env bash
# Server-side deploy for budget.dibberlab.me.
#
# GitHub Actions runs this over SSH after every push to main. The deploy key in
# /root/.ssh/authorized_keys is locked to this one script, so the key can't be
# used for anything else.
#
# Install (once, on the droplet):
#   install -m 755 /var/www/budget/deploy/budget-deploy.sh /usr/local/bin/budget-deploy
#
# The installed copy lives outside the repo on purpose: a push to main can't
# change what root runs here. Re-run the install line if you edit this file.
set -euo pipefail

APP_DIR=/var/www/budget
cd "$APP_DIR"

before=$(git rev-parse HEAD)
git pull --ff-only origin main
after=$(git rev-parse HEAD)
echo ">>> $before -> $after"

# Only reinstall packages when requirements.txt changed.
if ! git diff --quiet "$before" "$after" -- requirements.txt; then
    echo ">>> requirements.txt changed, installing packages"
    .venv/bin/pip install --quiet -r requirements.txt
fi

# Restart even for template-only changes: templates are cached.
systemctl restart budget

# Wait for gunicorn to answer, then fail loudly if it doesn't.
for _ in $(seq 1 15); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || true)
    if [ -n "$code" ] && [ "$code" != "000" ] && [ "$code" -lt 500 ]; then
        echo ">>> budget is up (HTTP $code) at $(git log -1 --format='%h %s')"
        exit 0
    fi
    sleep 2
done

echo "!!! budget did not come back up. Recent logs:"
journalctl -u budget --since "-2min" --no-pager | tail -40
exit 1
