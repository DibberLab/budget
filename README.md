# Household Budget

A self-hosted budgeting app for two-person households, inspired by YNAB and Mint. Runs on a VPS behind nginx/Caddy. Zero recurring fees beyond your server cost.

> **Hey Colin** 👋 — Andy built this, then had an AI help him make it look way more impressive than it actually is. It's essentially a very elaborate way to track how much he spends at Menards. You can use it too. Just don't tell him how easy it was to set up or he'll feel weird about it. Also he probably still owes you money from something — this app will not help him remember that.

---

## What it does

- **Hybrid envelope budgeting** — assign every dollar to a category, with carryover from month to month (YNAB-style), plus Mint-style reports on top.
- **Two-user authentication** — exactly two logins, hard-capped at the model layer. Shared household data; either user sees everything.
- **Multiple financial accounts** — checking, savings, credit cards, cash, plus off-budget investment/loan accounts. One-click transfers.
- **Three ways to ingest transactions** — manual entry, CSV import (auto-detects most bank formats and de-duplicates), or optional Plaid sync (automatic bank connection).
- **Savings goals** — target amount + date, log contributions, progress bars.
- **Recurring transactions** — schedule bills and paychecks on any cadence; the processor catches up automatically.

---

## Features

### Home / Dashboard
The home page combines a spending reports view and a calendar in one place:

- **Spending by Category** and **Spending by Payee** doughnut charts with configurable date ranges (Last 30/90/180/365 days, This Month, Last Month, Last 3/6/12/24 months, This Year, Last Year, Custom).
- **Monthly Spend by Category** stacked bar chart for trend analysis.
- **Categories tab** — grouped progress bars showing budget vs. spent for the current month, with avg/month, last month, and budgeted columns. Budget amounts are **inline-editable** directly from this table.
- **Payees tab** — same layout, organized by payee with spend totals and averages.
- Hover over any category or payee row to see a tooltip with its 8 most recent transactions (including memos).
- Click any category or payee to open a transaction panel for the selected date range — all fields **inline-editable** (payee, memo, amount, category, date, account).
- **FullCalendar** view showing all transactions color-coded by category, with daily net totals on each cell. Optionally overlays your Google Calendar events.
- Clicking a calendar transaction navigates to the Transactions page pre-filtered by that category.

### Transactions
- Full transaction list with inline editing for every field.
- **Tag cloud filter** — click any combination of category and payee pills to filter instantly, with live counts that update as you select.
- **Date range filter** — same presets as the home page plus a custom date picker. All client-side, instant filtering with no page reload.
- Summary bar showing visible transaction count, total expenses, total income, and net — updates live as filters change.
- Clicking the colored dot or ⊕ icon on any row adds that category or payee to the active tag filter.

### Categories
- Inline-editable category names — click any name to rename it.
- Inline-editable group headers — click a group name to rename the entire group at once.
- Move categories between groups via the group dropdown on each row.
- Income toggle checkbox per category.
- Add new categories with auto-suggest for existing group names.

### Budget Page
- YNAB-style envelope budgeting by month.
- "To Be Budgeted" computed live from income minus assigned amounts.
- Click any budgeted amount to edit it inline.
- Navigate between months with prev/next arrows.

### Plaid (Automatic Bank Sync)
- Connect your real bank account via [Plaid](https://plaid.com). Supports production (real banks) and sandbox (test credentials) environments.
- After linking, hit **Sync now** on the Plaid page to pull in new transactions.
- Works with most US banks. OAuth banks (Chase, BofA, etc.) redirect through the bank's login page.

### Other Pages
- **Accounts** — manage accounts, mark as closed/off-budget, view balances.
- **Goals** — savings goals with target amount, target date, and contribution tracking.
- **Recurring** — scheduled transactions (bills, paychecks) on any interval.
- **Import CSV** — paste or upload a bank CSV; the importer auto-detects column formats and skips duplicates.

---

## Setup

### Requirements
- Python 3.12+
- A Linux server (Ubuntu 24.04 recommended)
- Optional: a [Plaid](https://dashboard.plaid.com/signup) account (free sandbox tier works for testing)
- Optional: a public Google Calendar ICS URL

### Environment variables

Copy `.env.example` to `.env` and fill in:

```bash
SECRET_KEY=<long random hex string>   # required — generate with: openssl rand -hex 32

# Optional — Plaid bank sync
PLAID_CLIENT_ID=...
PLAID_SECRET=...
PLAID_ENV=production                  # or "sandbox" for testing with fake credentials

# Optional — Google Calendar overlay on the home calendar
GOOGLE_CALENDAR_ICS=https://calendar.google.com/calendar/ical/...
```

The app works fine without Plaid or Google Calendar — those features simply won't appear.

### Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_ENV=development
export FLASK_APP=app.py
export SECRET_KEY=$(openssl rand -hex 32)
flask run --debug
```

Open <http://127.0.0.1:5000>. The dev database lives at `instance/budget.db`.

### Production (systemd + nginx or Caddy)

Full step-by-step in **[DEPLOY.md](DEPLOY.md)**. The short version:

1. Clone the repo to your server
2. Create `.env` with a strong `SECRET_KEY`
3. Install dependencies into a virtualenv
4. Set up a systemd service (see `deploy/budget.service`)
5. Point nginx or Caddy at `127.0.0.1:8000`
6. Add a nightly backup cron (see `scripts/backup.sh`)

---

## Architecture

```
                Internet
                    │
                    ▼
        ┌─────────────────────┐
        │  nginx / Caddy      │   :443 / :80
        │  (TLS, HSTS)        │
        └──────────┬──────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ gunicorn → Flask    │   127.0.0.1:8000
        │  • Auth (2 users)   │
        │  • CSRF, rate limit │
        │  • Envelope budget  │
        └──────────┬──────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ SQLite              │   /var/lib/budget/budget.db
        │                     │   nightly .backup → gzipped snapshot
        └─────────────────────┘
```

### File layout

| File | Purpose |
| --- | --- |
| `app.py` | Flask app factory, all routes, query helpers. |
| `auth.py` | Login/logout/setup/user-management blueprint. |
| `models.py` | SQLAlchemy models. All money stored as integer cents. |
| `csv_import.py` | Bank CSV parser with column auto-detection and deduplication. |
| `plaid_client.py` | Optional Plaid integration (link tokens, token exchange, sync). |
| `recurring.py` | Posts due recurring transactions on each authenticated request. |
| `templates/` | Jinja2 templates — Bootstrap 5.3, Chart.js 4.4, FullCalendar 6. |
| `static/style.css` | Custom CSS on top of Bootstrap. |
| `deploy/budget.service` | systemd unit file. |
| `scripts/backup.sh` | Nightly sqlite3 `.backup` → gzipped snapshot, 30-day retention. |
| `.env.example` | All supported env vars with descriptions; copy to `.env`. |

### Data model notes

All amounts are stored as **integer cents** — `$4.75` is stored as `475`. This eliminates floating-point drift.

`Transaction.amount` is signed: negative = outflow, positive = inflow. Transfers are two transactions linked by a `transfer_id` UUID.

`BudgetMonth` rows hold the envelope assignment per `(year, month, category_id)`. Available balance is computed live:

```
available = carryover + assigned + spent     (spent is always negative)
```

Surplus rolls forward automatically. Overspending reduces next month's "To Be Budgeted."

---

## Security

- HTTPS via Caddy auto-TLS or your own cert
- Secure HttpOnly SameSite=Lax session cookies
- Scrypt-hashed passwords (10-char minimum enforced)
- CSRF protection on every form and every AJAX POST
- Rate-limited login endpoint (10 attempts / 5 min / IP)
- App refuses to start without a real `SECRET_KEY`
- All secrets in `.env` (gitignored); nothing sensitive hardcoded

---

## CLI

Run with the virtualenv active:

| Command | What it does |
| --- | --- |
| `flask create-user email@x.com --admin` | Create a user (hard cap: 2 total). |
| `flask reset-password email@x.com` | Reset a password from the shell. |
