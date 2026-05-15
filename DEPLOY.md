# Deploying Household Budget to DigitalOcean

This is a step-by-step guide to deploy the app at **budget.dibberlab.me** on a small DigitalOcean droplet using Docker Compose, with Caddy handling automatic HTTPS via Let's Encrypt and nightly local backups.

Estimated total cost: **$6/month** for a Basic Regular droplet (1 GB / 1 vCPU / 25 GB SSD), which is more than enough for a two-user household app.

## 1. Create the droplet

1. In the DigitalOcean dashboard, click **Create → Droplets**.
2. Choose:
   - **Image:** Ubuntu 24.04 LTS x64
   - **Size:** Basic / Regular Disk / $6 plan (1 GB RAM, 1 vCPU)
   - **Datacenter:** whatever region is closest to you
   - **Authentication:** SSH key (paste your public key — much safer than passwords)
   - **Hostname:** `budget` (or whatever you like)
3. Click **Create Droplet** and note the IPv4 address.

## 2. Point DNS at the droplet

In your domain registrar / DNS provider for `dibberlab.me`:

- Create an **A record** for `budget` pointing to the droplet's IPv4 address
- TTL: 300 seconds is fine

Confirm propagation before continuing:

```bash
dig +short budget.dibberlab.me
# Should print the droplet IP
```

Caddy can't issue a TLS cert until this resolves correctly.

## 3. Harden the droplet

SSH in as root (or your sudo user):

```bash
ssh root@<droplet-ip>
```

Update + install Docker + create a non-root user. Run these commands one block at a time so any failure stops you:

```bash
# Patch the OS
apt-get update && apt-get -y upgrade

# Create a non-root user that owns the app
adduser --gecos "" --disabled-password budget
usermod -aG sudo budget
# Copy your SSH key so you can log in as that user too
mkdir -p /home/budget/.ssh
cp ~/.ssh/authorized_keys /home/budget/.ssh/
chown -R budget:budget /home/budget/.ssh
chmod 700 /home/budget/.ssh && chmod 600 /home/budget/.ssh/authorized_keys

# Firewall: allow SSH, HTTP, HTTPS only
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable

# Install Docker
curl -fsSL https://get.docker.com | sh
usermod -aG docker budget

# Optional: disable password SSH and root login altogether
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl restart ssh
```

Log out and back in as the `budget` user:

```bash
ssh budget@<droplet-ip>
```

## 4. Drop the app onto the droplet

The simplest way is git, but `scp` works equally well.

```bash
# Option A: clone from a git repo you've pushed the code to
sudo mkdir -p /opt/budget && sudo chown budget:budget /opt/budget
cd /opt/budget
git clone <your-repo-url> .

# Option B: scp from your laptop
scp -r ./budget_app/* budget@<droplet-ip>:/opt/budget/
```

## 5. Configure secrets

```bash
cd /opt/budget
cp .env.example .env
# Generate a strong SECRET_KEY
python3 -c 'import secrets; print("SECRET_KEY=" + secrets.token_hex(32))' >> .env.tmp
# Replace the placeholder line in .env with the new line
grep -v '^SECRET_KEY=' .env > .env.new && cat .env.tmp >> .env.new && mv .env.new .env && rm .env.tmp
# Tighten permissions on the secret file
chmod 600 .env
```

Open `.env` and double-check `SECRET_KEY` is set to a real long random hex string.

## 6. First deploy

```bash
cd /opt/budget
docker compose build
docker compose up -d
```

Watch the logs while Caddy provisions the TLS cert (this can take 30–90 seconds the first time):

```bash
docker compose logs -f caddy
# Press Ctrl-C when you see "certificate obtained successfully" for budget.dibberlab.me
```

Verify the site is up:

```bash
curl -I https://budget.dibberlab.me/healthz
# Expect: HTTP/2 200
```

## 7. Create the two users

Visit **https://budget.dibberlab.me** in your browser. Because the database is empty, you'll be redirected to `/setup` — fill in your admin credentials. Once you're signed in, click your name in the top-right → **Manage users**, and invite your wife from there.

If you'd rather create users from the command line (e.g. recovering access):

```bash
cd /opt/budget
docker compose exec app flask create-user andy@example.com --admin
docker compose exec app flask create-user wife@example.com
```

## 8. Schedule nightly backups

```bash
# Make the backup script executable
chmod +x /opt/budget/scripts/backup.sh

# Make sure /var/backups/budget exists and the budget user can write to it
sudo mkdir -p /var/backups/budget /var/log
sudo chown budget:budget /var/backups/budget

# Add a cron entry (runs at 03:00 local time daily)
crontab -e
```

Add this line:

```cron
0 3 * * * /opt/budget/scripts/backup.sh >> /var/log/budget-backup.log 2>&1
```

Force a backup now to confirm it works:

```bash
/opt/budget/scripts/backup.sh
ls -la /var/backups/budget/
```

You should see a `.db.gz` file from today.

### Restoring from a backup

```bash
cd /opt/budget
# Stop the app so nothing writes to the DB
docker compose stop app
# Extract the snapshot
gunzip -c /var/backups/budget/2026-05-14.db.gz > /tmp/restore.db
# Copy it into the named volume
docker run --rm -v budget_budget-data:/data -v /tmp:/restore alpine \
    sh -c 'cp /restore/restore.db /data/budget.db && chown 1000:1000 /data/budget.db'
docker compose start app
```

## 9. Day-2 operations

Useful commands once you're up and running:

```bash
# Update to a new version
cd /opt/budget
git pull                   # or scp the new files
docker compose build
docker compose up -d

# Tail application logs
docker compose logs -f app

# Tail Caddy/HTTP access logs
docker compose logs -f caddy

# Reset a forgotten password
docker compose exec app flask reset-password wife@example.com

# Inspect the database directly
docker compose exec app sqlite3 /data/budget.db
```

## 10. Hardening checklist (optional but recommended)

- **Enable automatic security updates:** `sudo apt-get install -y unattended-upgrades`
- **Fail2ban for SSH:** `sudo apt-get install -y fail2ban`
- **Disable docker compose port 80 redirect bypass:** already done by Caddy's HTTPS-by-default behavior.
- **Verify HSTS preload eligibility:** once you've run with HTTPS for a week, you can submit your domain to [hstspreload.org](https://hstspreload.org).
- **Offsite backups:** if you want belt-and-suspenders, add a second cron line that uses `rclone` to push `/var/backups/budget/` to Backblaze B2 (~$0.005/GB/mo).

## Troubleshooting

- **"connection refused"** on first hit → check `docker compose ps` shows both `app` and `caddy` as healthy.
- **"This site can't be reached"** → DNS hasn't propagated yet. `dig +short budget.dibberlab.me` should return your droplet IP.
- **Caddy can't get cert** → make sure ports 80 + 443 are open in `ufw` and you don't have any other process bound to them.
- **`SECRET_KEY environment variable is required`** in logs → `.env` is missing or wasn't picked up. `docker compose config` will show what env vars resolved.
- **CSRF errors** when submitting forms → the app uses Flask-WTF; a stale browser tab from before deploy can show this. Hard-refresh.
