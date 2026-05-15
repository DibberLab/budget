# Deploying Household Budget — nginx + Cloudflare stack

This is for the existing stack: DigitalOcean droplet, nginx as the reverse proxy, Cloudflare in front. Code lives at `/var/www/budget`. The app runs as a systemd service exposing gunicorn on `127.0.0.1:8000`, nginx proxies to it, and Cloudflare terminates TLS at the edge.

If you're not using this stack, see `DEPLOY.md` for the Docker + Caddy version instead.

## Architecture

```
visitor → Cloudflare (TLS) → nginx :80 → gunicorn :8000 → SQLite (/var/lib/budget/budget.db)
```

Cloudflare handles HTTPS for users; the link between Cloudflare and nginx is HTTP. nginx forwards to gunicorn over the loopback interface. SQLite lives at `/var/lib/budget/budget.db` (separated from the code directory so a `git pull` can't accidentally clobber data).

## One-time setup

### 1. Install system packages

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip sqlite3
```

### 2. Create the data directory and the venv

```bash
# Data lives outside /var/www so deploys don't touch it.
sudo mkdir -p /var/lib/budget /var/log/budget /var/backups/budget
sudo chown www-data:www-data /var/lib/budget /var/log/budget /var/backups/budget

# Build the venv inside /var/www/budget; the systemd unit looks for .venv there.
cd /var/www/budget
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt

# Make sure www-data can read everything (the systemd unit runs as www-data).
sudo chown -R www-data:www-data /var/www/budget
```

### 3. Create the `.env` file

```bash
cd /var/www/budget
sudo cp .env.example .env
# Generate a real SECRET_KEY:
echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
    | sudo tee /tmp/secret.line > /dev/null
sudo sed -i "/^SECRET_KEY=/d" .env
sudo tee -a .env < /tmp/secret.line > /dev/null
sudo rm /tmp/secret.line
sudo chown www-data:www-data .env
sudo chmod 600 .env
```

Verify:

```bash
sudo cat /var/www/budget/.env | head -3
# SECRET_KEY=<64 hex chars>
```

### 4. Install the systemd service

```bash
sudo cp /var/www/budget/deploy/budget.service /etc/systemd/system/budget.service
sudo systemctl daemon-reload
sudo systemctl enable --now budget.service
sudo systemctl status budget.service
```

You should see `active (running)`. If it crashes, `sudo journalctl -u budget.service -n 100` will tell you why — the two most common reasons are missing `SECRET_KEY` (it refuses to start) and wrong file permissions on `/var/lib/budget/`.

Smoke test from the droplet itself:

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

### 5. Swap in the corrected nginx config

Your current config has three issues that will bite you in production: `X-Forwarded-Proto $scheme` will break secure-cookie sessions (because nginx sees HTTP from Cloudflare and Flask will think the connection is insecure), `Connection: "upgrade"` is set for every request even though there are no websockets, and there's no `client_max_body_size` so 16 MB CSV imports get truncated at nginx's 1 MB default.

```bash
# Back up the old config so you can roll back fast.
sudo cp /etc/nginx/sites-available/budget /etc/nginx/sites-available/budget.bak.$(date +%F)

sudo cp /var/www/budget/deploy/budget.nginx /etc/nginx/sites-available/budget
sudo nginx -t          # syntax check
sudo systemctl reload nginx
```

The new config proxies to port **8000**, not 5000. Gunicorn now listens on 8000, leaving 5000 free for any future Flask dev runs.

### 6. Install the Cloudflare real-IP snippet

Without this, every visitor looks like a Cloudflare IP to nginx and the Flask rate limiter, which makes brute-force protection useless.

```bash
sudo cp /var/www/budget/deploy/cloudflare-real-ip.conf /etc/nginx/conf.d/cloudflare-real-ip.conf
sudo nginx -t
sudo systemctl reload nginx
```

The IP ranges in that file are current as of mid-2026; refresh them every few months. There's a script comment at the top of the file showing how to auto-refresh from `https://www.cloudflare.com/ips-v4`.

### 7. First-run setup in the browser

Hit **<https://budget.dibberlab.me>**. Because the database is empty, you'll land on `/setup` — fill in your admin credentials. Sign in, click your name in the top-right → **Manage users**, and invite your wife from there.

If you'd rather create users from the CLI:

```bash
cd /var/www/budget
sudo -u www-data .venv/bin/flask create-user andy@example.com --admin
sudo -u www-data .venv/bin/flask create-user wife@example.com
```

### 8. Schedule nightly backups

```bash
sudo chmod +x /var/www/budget/scripts/backup.sh

# Add to root's crontab (runs at 03:00 daily)
sudo crontab -e
```

Add the line:

```cron
0 3 * * *  /var/www/budget/scripts/backup.sh >> /var/log/budget-backup.log 2>&1
```

Force a run to confirm:

```bash
sudo /var/www/budget/scripts/backup.sh
ls -la /var/backups/budget/
```

You should see a `.db.gz` file from today.

### Restoring from a backup

```bash
# Stop the app so nothing's writing to the DB.
sudo systemctl stop budget.service
# Extract the snapshot over the live file.
sudo gunzip -c /var/backups/budget/2026-05-14.db.gz | sudo tee /var/lib/budget/budget.db > /dev/null
sudo chown www-data:www-data /var/lib/budget/budget.db
sudo systemctl start budget.service
```

## Cloudflare SSL mode — *please read this*

Your current setup has nginx on plain HTTP. That means Cloudflare is operating in **Flexible** SSL mode: HTTPS is real for the visitor's browser, but the hop between Cloudflare and your droplet is plaintext. Anyone with access to the network path between Cloudflare and DigitalOcean (in practice: nobody, but theoretically a state actor or a compromised network hop) could read sessions and passwords.

For a household budget you probably don't lose sleep over this, but it's a trivial upgrade. Pick whichever:

- **Full mode + Cloudflare Origin Certificate (recommended).** Generate a free origin cert from the Cloudflare dashboard that's valid for 15 years and only trusted by Cloudflare, then add a `listen 443 ssl` block to nginx pointing at the cert. Cloudflare flips to Full mode and the origin hop becomes encrypted. Walk-through:
   1. Cloudflare dashboard → SSL/TLS → Origin Server → **Create Certificate** (defaults are fine).
   2. Save the cert to `/etc/ssl/cloudflare/budget.dibberlab.me.pem` and the key to `/etc/ssl/cloudflare/budget.dibberlab.me.key` (root, mode 600).
   3. Add to your nginx server block:
      ```nginx
      listen 443 ssl http2;
      listen [::]:443 ssl http2;
      ssl_certificate     /etc/ssl/cloudflare/budget.dibberlab.me.pem;
      ssl_certificate_key /etc/ssl/cloudflare/budget.dibberlab.me.key;
      ```
   4. Cloudflare dashboard → SSL/TLS → Overview → switch to **Full (strict)**.
- **Stay on Flexible.** Works fine. The hardcoded `X-Forwarded-Proto https` in the new nginx config means the Flask app still treats requests as secure, so cookies + redirects behave correctly.

The nginx config I gave you works for both modes — you only need to add the 443 block above if you want Full mode.

## Day-2 operations

```bash
# Deploy a new version
cd /var/www/budget
sudo -u www-data git pull          # or however you ship code
sudo -u www-data .venv/bin/pip install -r requirements.txt
sudo systemctl restart budget.service

# Tail application logs
sudo journalctl -u budget.service -f

# Tail nginx access / error logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log

# Reset a forgotten password
cd /var/www/budget
sudo -u www-data .venv/bin/flask reset-password wife@example.com

# Inspect the database directly
sudo -u www-data sqlite3 /var/lib/budget/budget.db
```

## Troubleshooting

- **`SECRET_KEY environment variable is required`** in journal → the `.env` file isn't being read. Confirm `EnvironmentFile=/var/www/budget/.env` in the unit, check ownership (`ls -l /var/www/budget/.env`), and that the file has no quotes around the value.
- **502 from nginx** → gunicorn isn't running or isn't on 127.0.0.1:8000. `sudo systemctl status budget.service` and `sudo ss -ltnp | grep 8000`.
- **Login form bounces with "Invalid email or password"** even on correct creds → secure-cookie issue. Confirm the new nginx config is loaded (`grep X-Forwarded-Proto /etc/nginx/sites-available/budget` should show the hardcoded `https`).
- **CSRF errors after a deploy** → a stale browser tab from before the deploy still has an old token. Hard-refresh.
- **"Too many requests" lockout** → Flask-Limiter is rate-limiting on the wrong IP because the Cloudflare real-IP snippet isn't loaded. Verify with `curl -H "Host: budget.dibberlab.me" http://127.0.0.1/...` from the droplet and check the journal.
- **Permission denied writing to /var/lib/budget/budget.db** → the systemd unit runs as `www-data`. `sudo chown -R www-data:www-data /var/lib/budget`.
