# Household Budget

A self-hosted budgeting app for two-person households, inspired by YNAB and Mint. Runs in Docker behind Caddy on your own server. Zero recurring fees beyond the $6/mo droplet.

## What it does

- **Hybrid envelope budgeting** — assign every dollar to a category, with carryover from month to month (YNAB-style), plus Mint-style reports on top.
- **Two-user authentication** — exactly two logins, hard-capped at the model layer. Shared household data; either user sees everything.
- **Multiple financial accounts** — checking, savings, credit cards, cash, plus off-budget investment/loan accounts. One-click transfers.
- **Three ways to ingest transactions** — manual entry, CSV import (auto-detects most bank formats and de-duplicates), or optional Plaid sync.
- **Reports & charts** — income vs. expense, spending by category, net worth trend.
- **Savings goals** — target amount + date, log contributions, progress bars.
- **Recurring transactions** — schedule bills and paychecks on any cadence; the processor catches up automatically.

## Production security

- HTTPS everywhere via Caddy auto-TLS (Let's Encrypt)
- Secure HttpOnly SameSite=Lax session cookies
- Scrypt-hashed passwords (10-char minimum)
- CSRF protection on every form and AJAX call
- Rate-limited login (10 attempts per 5 minutes per IP)
- Strong HSTS, X-Frame-Options=DENY, X-Content-Type-Options=nosniff, Permissions-Policy
- Non-root container user
- Configuration via env vars; refuses to start without a real `SECRET_KEY`

## Quick deploy (production)

Full step-by-step instructions live in **[DEPLOY.md](DEPLOY.md)**. The short version:

```bash
# On a fresh Ubuntu 24.04 droplet, as a non-root user with Docker installed:
git clone <your-repo> /opt/budget && cd /opt/budget
cp .env.example .env && nano .env   # set SECRET_KEY to a long random hex
docker compose up -d --build
# Open https://budget.dibberlab.me, complete first-run setup,
# invite your second user from the Manage Users page.
```

Then add the nightly backup cron entry (DEPLOY.md §8).

## Local development

```bash
cd budget_app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_ENV=development            # uses a dev SECRET_KEY, disables HTTPS-only cookies
export FLASK_APP=app.py
flask run --debug
```

Then open <http://127.0.0.1:5000>. Your dev database lives at `instance/budget.db`.

## Architecture

```
                Internet
                    │
                    ▼
        ┌─────────────────────┐
        │ Caddy (TLS, HSTS)   │   :443 / :80   (auto Let's Encrypt)
        └──────────┬──────────┘
                   │ internal docker network
                   ▼
        ┌─────────────────────┐
        │ gunicorn → Flask    │   :8000
        │  • Auth (2 users)   │
        │  • CSRF, rate limit │
        │  • Envelope budget  │
        └──────────┬──────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ SQLite              │   /data/budget.db
        │ (named volume)      │   nightly .backup → /var/backups/budget/
        └─────────────────────┘
```

### File layout

| File | Purpose |
| --- | --- |
| `app.py` | Flask app factory, routes, query helpers, CLI commands. |
| `auth.py` | Login/logout/setup/user-management blueprint. |
| `models.py` | SQLAlchemy models. All money in integer cents. |
| `csv_import.py` | Bank CSV parser with column auto-detection + dedupe. |
| `plaid_client.py` | Optional Plaid sync. |
| `recurring.py` | Posts due recurring transactions on each authenticated request. |
| `templates/` | Jinja templates, Bootstrap 5. |
| `static/style.css` | Custom CSS on top of Bootstrap. |
| `Dockerfile` | Multi-stage build → slim gunicorn runtime. |
| `docker-compose.yml` | Two services: `app` + `caddy`. SQLite in a named volume. |
| `Caddyfile` | TLS, security headers, reverse proxy. |
| `scripts/backup.sh` | Nightly sqlite3 `.backup` → gzipped snapshot, 30-day retention. |
| `.env.example` | Required env vars; copy to `.env`. |
| `DEPLOY.md` | Production deployment guide. |

### Data model notes

All amounts are integer cents — `$4.75` is stored as `475`. This eliminates floating-point drift across millions of transactions.

`Transaction.amount` is signed: negative = outflow, positive = inflow. Transfers are two transactions linked by `transfer_id` (a UUID).

`BudgetMonth` rows hold the envelope assignment per (year, month, category). "Available" in any month is computed live:

```
available = carryover + assigned + spent     (spent is negative)
carryover = Σ prior_assigned + Σ prior_spent
```

This means surplus rolls forward, overspending eats into next month's To Be Budgeted, and the math always matches what you'd compute by hand.

## CLI commands

Run from the host with `docker compose exec app flask <command>`:

| Command | What it does |
| --- | --- |
| `flask create-user email@x.com --admin` | Create a user (refuses past the 2-user cap). |
| `flask reset-password email@x.com` | Reset someone's password from the shell. |

## What's deliberately not in v1

- Investment positions with real-time prices
- Tax-lot accounting
- Multi-household / cross-tenant data isolation (you'd need this for SaaS)
- Mobile app (the web UI is responsive but not native)

The schema and patterns here scale to all of these, but they're sensible v2 work.
