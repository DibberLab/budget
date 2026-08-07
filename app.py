"""
Flask app factory and HTTP routes.

This is the production entry point. Configuration comes from environment
variables; the app refuses to start without a real SECRET_KEY.

When deployed:
  - gunicorn runs this app behind Caddy
  - Caddy terminates TLS and forwards to gunicorn on the internal Docker network
  - ProxyFix tells Flask the real scheme/host so url_for builds https URLs
  - Sessions are Secure + HttpOnly + SameSite=Lax cookies
"""
import hashlib
import json
import os
import secrets
import sys
import uuid
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

import anthropic
import click
from flask import (Flask, Response, abort, current_app, flash, jsonify,
                   redirect, render_template, request, stream_with_context,
                   url_for)
from flask_login import current_user, login_required
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import and_, extract, func, or_
from sqlalchemy.orm import joinedload
from werkzeug.middleware.proxy_fix import ProxyFix

import csv_import
import plaid_client
from auth import auth_bp, login_manager, setup_required
from models import (MAX_USERS, Account, AiCategorySuggestion,
                    AiPayeeSuggestion, BudgetMonth, Category, Goal,
                    PlaidItem, Recurring, Transaction, User, db, fmt_money,
                    from_cents, to_cents)
from recurring import post_due_recurring


csrf = CSRFProtect()
limiter = Limiter(get_remote_address, default_limits=[])

# ---------- deterministic group colours ----------
# Perceptually distinct palette (Tableau-10). Hash the group name so every
# group always gets the same colour regardless of query order or page.
_GROUP_PALETTE = [
    "#4e79a7",  # blue
    "#f28e2b",  # orange
    "#e15759",  # red
    "#76b7b2",  # teal
    "#59a14f",  # green
    "#edc948",  # yellow-gold
    "#b07aa1",  # purple
    "#ff9da7",  # pink
    "#9c755f",  # brown
    "#bab0ac",  # grey
]

def _group_color(group_name: str) -> str:
    """Return a stable hex colour for a group name."""
    idx = int(hashlib.md5(group_name.encode()).hexdigest(), 16) % len(_GROUP_PALETTE)
    return _GROUP_PALETTE[idx]


# ---------- Anthropic AI helpers ----------

_ai_client: anthropic.Anthropic | None = None


def _get_ai_client() -> anthropic.Anthropic:
    global _ai_client
    if _ai_client is None:
        api_key = os.environ.get("CLAUDE_API_KEY")
        if not api_key:
            raise RuntimeError("CLAUDE_API_KEY environment variable is not set")
        _ai_client = anthropic.Anthropic(api_key=api_key)
    return _ai_client


def _ai_tool_call(prompt: str, tool_name: str, tool_description: str,
                  input_schema: dict, max_tokens: int = 8192) -> dict:
    """Call Claude and force a structured response via tool-use, so the reply
    is schema-validated JSON straight from the API — no brittle scraping of
    free-form text for a '{...}' substring, which breaks the moment a payee
    name or memo contains a quote/brace and desyncs the parse.

    Forced tool-use has no scratch space to spill into once max_tokens is hit
    mid-JSON — the SDK just hands back an incomplete/empty `.input` with no
    error. On a big household's payee/transaction list that silently looked
    identical to "Claude found nothing," which is how this went unnoticed:
    stop_reason is checked explicitly so a truncation surfaces as a real error
    instead of a quiet, wrong "no suggestions."""
    response = _get_ai_client().messages.create(
        model="claude-opus-4-8",
        max_tokens=max_tokens,
        tools=[{
            "name": tool_name,
            "description": tool_description,
            "input_schema": input_schema,
        }],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's response was cut off before it finished (hit the token limit). "
            "Try again — if this keeps happening, the list may need to be analyzed in smaller batches."
        )
    block = next((b for b in response.content if b.type == "tool_use"), None)
    return block.input if block else {}


def _build_budget_context() -> str:
    """Gather recent transaction data and budget summary for Claude's system prompt."""
    today = date.today()
    ninety_days_ago = today - timedelta(days=90)

    # Monthly income/expense for last 6 months
    months = []
    y, m = today.year, today.month
    for _ in range(6):
        months.append((y, m))
        y, m = _prev_month(y, m)
    months.reverse()

    lines = [f"Today's date: {today.isoformat()}", ""]

    lines.append("## Monthly Income vs Expenses (last 6 months)")
    lines.append("Month      | Income      | Expenses    | Net")
    lines.append("-----------|-------------|-------------|------------")
    for yy, mm in months:
        inc = _income_total(yy, mm)
        _, spent = _month_totals(yy, mm)
        net = inc + spent  # spent is negative, so net = income - abs(expenses)
        lines.append(
            f"{yy}-{mm:02d}     | ${inc/100:>10,.2f} | ${abs(spent)/100:>10,.2f} | ${net/100:>10,.2f}"
        )

    # Category spending current month
    cur_cats = (
        db.session.query(Category.group_name, Category.name,
                         func.coalesce(func.sum(Transaction.amount), 0))
        .join(Transaction, Transaction.category_id == Category.id)
        .filter(
            extract("year", Transaction.date) == today.year,
            extract("month", Transaction.date) == today.month,
            Category.is_income.is_(False),
        )
        .group_by(Category.id)
        .order_by(func.sum(Transaction.amount))
        .all()
    )
    lines += ["", f"## Spending by Category ({today.year}-{today.month:02d})"]
    for group, name, total in cur_cats:
        lines.append(f"  {group} / {name}: ${abs(int(total))/100:,.2f}")

    # Top 30 payees last 90 days (outflows only). parent_id filter keeps
    # split line-items from being double-counted alongside their parent.
    payee_rows = (
        db.session.query(
            Transaction.payee,
            func.count().label("cnt"),
            func.sum(Transaction.amount).label("tot"),
        )
        .filter(Transaction.date >= ninety_days_ago, Transaction.amount < 0,
                Transaction.parent_id.is_(None))
        .group_by(Transaction.payee)
        .order_by(func.sum(Transaction.amount))
        .limit(30)
        .all()
    )
    lines += ["", "## Top Payees by Spending (last 90 days)"]
    for payee, cnt, tot in payee_rows:
        lines.append(f"  {payee or '(blank)'}: ${abs(int(tot))/100:,.2f} ({cnt} transactions)")

    # Recent transactions (last 90 days, capped at 250)
    recent = (
        Transaction.query
        .filter(Transaction.date >= ninety_days_ago, Transaction.parent_id.is_(None))
        .options(joinedload(Transaction.splits))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(250)
        .all()
    )
    lines += ["", "## Recent Transactions (last 90 days, newest first)"]
    lines.append("Date       | Account              | Category             | Payee                          | Amount")
    lines.append("-----------|----------------------|----------------------|--------------------------------|----------")
    for t in recent:
        if t.splits:
            cat = "Split (" + ", ".join(s.category.name if s.category else "?" for s in t.splits) + ")"
        else:
            cat = t.category.name if t.category else "(uncategorized)"
        acct = t.account.name if t.account else ""
        sign = "+" if t.amount > 0 else "-"
        amt = f"{sign}${abs(t.amount)/100:,.2f}"
        payee = (t.payee or "")[:32]
        lines.append(f"{t.date} | {acct[:20]:<20} | {cat[:20]:<20} | {payee:<32} | {amt}")

    # Active recurring items
    recurrings = Recurring.query.filter_by(active=True).all()
    if recurrings:
        lines += ["", "## Recurring / Scheduled Items"]
        for r in recurrings:
            cat = r.category.name if r.category else "(uncategorized)"
            sign = "+" if r.amount > 0 else "-"
            lines.append(
                f"  {r.payee or '(no name)'}: {sign}${abs(r.amount)/100:,.2f}/{r.frequency}, "
                f"next: {r.next_date}, category: {cat}"
            )

    # Goals
    goals = Goal.query.filter_by(completed=False).all()
    if goals:
        lines += ["", "## Savings Goals"]
        for g in goals:
            lines.append(
                f"  {g.name}: ${g.saved_amount/100:,.2f} saved / "
                f"${g.target_amount/100:,.2f} target ({g.percent_complete:.0f}%)"
            )

    return "\n".join(lines)


# ---------- factory ----------

def create_app():
    app = Flask(__name__)
    _configure(app)

    # Trust X-Forwarded-* headers from the reverse proxy. Without this,
    # url_for() builds http:// URLs and sessions break behind Caddy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Jinja helpers
    app.jinja_env.filters["money"] = fmt_money
    app.jinja_env.filters["dollars"] = from_cents
    app.jinja_env.globals["today"] = date.today

    app.register_blueprint(auth_bp)

    # Rate-limit the login POST to slow down credential stuffing: 10 attempts
    # per 5 minutes per IP. We attach AFTER blueprint registration so the
    # view function exists in app.view_functions.
    limiter.limit("10 per 5 minutes", methods=["POST"])(
        app.view_functions["auth.login"]
    )

    _register_cli(app)

    with app.app_context():
        db.create_all()
        _seed_default_categories()
        _migrate_owner_columns()
        _migrate_split_column()

    @app.before_request
    def _post_recurring():
        # Skip for static files & auth flow to keep those fast.
        if request.endpoint and not request.endpoint.startswith("static") \
                and not request.endpoint.startswith("auth."):
            try:
                if current_user.is_authenticated:
                    post_due_recurring()
            except Exception:
                db.session.rollback()

    register_routes(app)
    return app


def _configure(app: Flask):
    """Read configuration from environment variables. In production
    (FLASK_ENV != 'development'), missing SECRET_KEY is a fatal error."""
    secret = os.environ.get("SECRET_KEY")
    env = os.environ.get("FLASK_ENV", "production")

    if not secret:
        if env == "development":
            secret = "dev-only-do-not-use-in-production"
            print("WARNING: SECRET_KEY not set; using a development default.",
                  file=sys.stderr)
        else:
            print("FATAL: SECRET_KEY environment variable is required in production.",
                  file=sys.stderr)
            sys.exit(1)

    db_url = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(app.instance_path, 'budget.db')}",
    )
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 MB CSV upload cap

        # Session cookie hardening
        SESSION_COOKIE_SECURE=(env != "development"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        REMEMBER_COOKIE_SECURE=(env != "development"),
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_DURATION=timedelta(days=30),

        # CSRF: full session lifetime so background tabs don't expire mid-edit.
        WTF_CSRF_TIME_LIMIT=None,
    )


def _migrate_owner_columns():
    """Add owner_user_id / is_joint to transactions table for existing DBs."""
    from sqlalchemy import inspect as _sa_inspect, text as _text
    insp = _sa_inspect(db.engine)
    existing = {c["name"] for c in insp.get_columns("transactions")}
    with db.engine.connect() as conn:
        if "owner_user_id" not in existing:
            conn.execute(_text("ALTER TABLE transactions ADD COLUMN owner_user_id INTEGER REFERENCES users(id)"))
            conn.commit()
        if "is_joint" not in existing:
            conn.execute(_text("ALTER TABLE transactions ADD COLUMN is_joint BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()


def _migrate_split_column():
    """Add parent_id (split line-items) to transactions table for existing DBs."""
    from sqlalchemy import inspect as _sa_inspect, text as _text
    insp = _sa_inspect(db.engine)
    existing = {c["name"] for c in insp.get_columns("transactions")}
    with db.engine.connect() as conn:
        if "parent_id" not in existing:
            conn.execute(_text("ALTER TABLE transactions ADD COLUMN parent_id INTEGER REFERENCES transactions(id)"))
            conn.commit()
        conn.execute(_text("CREATE INDEX IF NOT EXISTS ix_txn_parent_id ON transactions(parent_id)"))
        conn.commit()


def _seed_default_categories():
    """Create starter categories and demo transactions if the DB is empty."""
    if Category.query.first():
        return

    # ---- categories ----
    cat_defs = [
        ("Income",      "Paycheck",        True),
        ("Income",      "Other Income",    True),
        ("Housing",     "Rent / Mortgage", False),
        ("Housing",     "Utilities",       False),
        ("Everyday",    "Groceries",       False),
        ("Everyday",    "Dining Out",      False),
        ("Everyday",    "Gas",             False),
        ("General",     "Shopping",        False),
        ("General",     "Entertainment",   False),
        ("General",     "Other",           False),
        ("Savings",     "Savings",         False),
    ]
    cats = {}
    for group, name, is_income in cat_defs:
        c = Category(group_name=group, name=name, is_income=is_income)
        db.session.add(c)
        cats[name] = c
    db.session.flush()  # get IDs without committing

    # ---- accounts ----
    checking = Account(name="Joint Checking", type="checking", starting_balance=250000)
    db.session.add(checking)
    db.session.flush()

    # ---- demo transactions (relative to today) ----
    today = date.today()

    def d(days_ago):
        return today - timedelta(days=days_ago)

    def txn(dt, payee, memo, amount_dollars, cat_name, account=None):
        db.session.add(Transaction(
            account_id  = (account or checking).id,
            date        = dt,
            payee       = payee,
            memo        = memo,
            amount      = int(amount_dollars * 100),
            category_id = cats[cat_name].id,
            source      = "manual",
        ))

    # -- two months ago --
    txn(d(62), "Employer",          "Paycheck",             2500,  "Paycheck")
    txn(d(58), "Landlord",          "Monthly rent",        -1200,  "Rent / Mortgage")
    txn(d(57), "Electric Company",  "Electric bill",         -88,  "Utilities")
    txn(d(55), "Grocery Store",     "Weekly groceries",      -94,  "Groceries")
    txn(d(52), "Gas Station",       "Fill up",               -55,  "Gas")
    txn(d(50), "Restaurant",        "Dinner out",            -62,  "Dining Out")
    txn(d(48), "Employer",          "Paycheck",             2500,  "Paycheck")
    txn(d(46), "Grocery Store",     "Weekly groceries",      -81,  "Groceries")
    txn(d(44), "Amazon",            "Household supplies",    -47,  "Shopping")
    txn(d(42), "Coffee Shop",       "Coffee",                -14,  "Dining Out")
    txn(d(40), "Gas Station",       "Fill up",               -52,  "Gas")
    txn(d(38), "Streaming Service", "Monthly subscription",  -18,  "Entertainment")

    # -- last month --
    txn(d(32), "Employer",          "Paycheck",             2500,  "Paycheck")
    txn(d(29), "Landlord",          "Monthly rent",        -1200,  "Rent / Mortgage")
    txn(d(28), "Electric Company",  "Electric bill",         -92,  "Utilities")
    txn(d(26), "Grocery Store",     "Weekly groceries",     -107,  "Groceries")
    txn(d(24), "Gas Station",       "Fill up",               -58,  "Gas")
    txn(d(22), "Restaurant",        "Date night",            -85,  "Dining Out")
    txn(d(20), "Employer",          "Paycheck",             2500,  "Paycheck")
    txn(d(18), "Grocery Store",     "Weekly groceries",      -76,  "Groceries")
    txn(d(16), "Department Store",  "Clothing",              -134, "Shopping")
    txn(d(14), "Coffee Shop",       "Coffee",                -11,  "Dining Out")
    txn(d(12), "Gas Station",       "Fill up",               -61,  "Gas")
    txn(d(10), "Streaming Service", "Monthly subscription",  -18,  "Entertainment")
    txn(d(10), "Friend",            "Venmo - owed money",     40,  "Other Income")

    # -- this month --
    txn(d(7),  "Employer",          "Paycheck",             2500,  "Paycheck")
    txn(d(6),  "Landlord",          "Monthly rent",        -1200,  "Rent / Mortgage")
    txn(d(5),  "Electric Company",  "Electric bill",         -79,  "Utilities")
    txn(d(4),  "Grocery Store",     "Weekly groceries",      -88,  "Groceries")
    txn(d(3),  "Gas Station",       "Fill up",               -54,  "Gas")
    txn(d(2),  "Restaurant",        "Lunch with coworkers",  -38,  "Dining Out")
    txn(d(1),  "Coffee Shop",       "Coffee",                -13,  "Dining Out")
    txn(d(0),  "Savings Account",   "Monthly transfer",     -200,  "Savings")

    # ---- budget for current month ----
    y, m = today.year, today.month
    budgets = {
        "Rent / Mortgage": 1200,
        "Utilities":        120,
        "Groceries":        350,
        "Dining Out":       150,
        "Gas":              120,
        "Shopping":         100,
        "Entertainment":     50,
        "Savings":          200,
    }
    for cat_name, amount in budgets.items():
        db.session.add(BudgetMonth(
            year=y, month=m,
            category_id=cats[cat_name].id,
            assigned=amount * 100,
        ))

    db.session.commit()


def _register_cli(app: Flask):
    """Add `flask` CLI commands for ops tasks."""

    @app.cli.command("create-user")
    @click.argument("email")
    @click.option("--password", prompt=True, hide_input=True,
                  confirmation_prompt=True)
    @click.option("--display-name", default="")
    @click.option("--admin/--no-admin", default=False,
                  help="Mark this user as the household admin.")
    def create_user(email, password, display_name, admin):
        """Create a household user from the command line.

        Useful if you ever need to bypass the web UI — e.g. recovering
        access after losing the admin password.
        """
        from models import User
        if User.query.count() >= MAX_USERS:
            click.echo(f"Refusing: this app is capped at {MAX_USERS} users.",
                       err=True)
            sys.exit(2)
        if User.query.filter_by(email=email.lower()).first():
            click.echo("Refusing: that email is already in use.", err=True)
            sys.exit(2)
        u = User(email=email.lower(),
                 display_name=display_name or email.split("@")[0],
                 is_admin=admin)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"Created user #{u.id}: {u.email} (admin={u.is_admin})")

    @app.cli.command("reset-password")
    @click.argument("email")
    @click.option("--password", prompt=True, hide_input=True,
                  confirmation_prompt=True)
    def reset_password(email, password):
        """Reset a user's password from the command line."""
        u = User.query.filter_by(email=email.lower()).first()
        if not u:
            click.echo("No such user.", err=True)
            sys.exit(2)
        u.set_password(password)
        db.session.commit()
        click.echo(f"Password reset for {u.email}.")


# ---------- helpers ----------

def _owner_filter_clauses(owner: str) -> list:
    """Return SQLAlchemy filter clauses for a given owner key.

    owner='all'   → no restriction
    owner='joint' → is_joint transactions only
    owner='<id>'  → that user's transactions (not joint)
    """
    if not owner or owner == "all":
        return []
    if owner == "joint":
        return [Transaction.is_joint.is_(True)]
    try:
        uid = int(owner)
        return [Transaction.owner_user_id == uid, Transaction.is_joint.is_(False)]
    except ValueError:
        return []


def _months_between(start_date, end_date) -> list:
    """List of (year, month) pairs whose calendar month overlaps the range."""
    months = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _compute_legend_groups(start_date, end_date, owner_clauses=None) -> list:
    """Category/group budget-vs-spend rows for the Budget tab and its
    Overview bar, scoped to [start_date, end_date] — so "Budgeted" reflects
    the same period as "Spent" instead of always comparing today's
    assignment against whatever period happens to be selected.

    avg_per_month/last_month stay anchored to "today": they're reference
    stats (all-time average, prior calendar month), not part of the
    period being viewed.
    """
    owner_clauses = owner_clauses or []
    today = date.today()
    lm_year, lm_month = _prev_month(today.year, today.month)
    lm_start = date(lm_year, lm_month, 1)
    lm_end   = date(lm_year, lm_month, monthrange(lm_year, lm_month)[1])

    categories = (Category.query
                  .filter_by(is_income=False, hidden=False)
                  .order_by(Category.group_name, Category.sort_order, Category.name)
                  .all())
    cat_ids_all = [c.id for c in categories]
    if not cat_ids_all:
        return []

    alltime_rows = (db.session.query(
            Transaction.category_id, func.sum(Transaction.amount), func.min(Transaction.date))
        .filter(Transaction.category_id.in_(cat_ids_all), Transaction.amount < 0)
        .group_by(Transaction.category_id).all())
    avg_map = {}
    for cid, total, first_date_ in alltime_rows:
        if first_date_:
            months_ = max(1, (today.year - first_date_.year) * 12
                          + (today.month - first_date_.month) + 1)
            avg_map[cid] = int(abs(total or 0) / months_)

    lm_map = {cid: int(abs(t or 0)) for cid, t in
              db.session.query(Transaction.category_id, func.sum(Transaction.amount))
              .filter(Transaction.category_id.in_(cat_ids_all), Transaction.amount < 0,
                      Transaction.date >= lm_start, Transaction.date <= lm_end)
              .group_by(Transaction.category_id).all()}

    months = _months_between(start_date, end_date)
    budget_map = {}
    if months:
        month_clause = or_(*[and_(BudgetMonth.year == y, BudgetMonth.month == m)
                             for y, m in months])
        for cid, a in (db.session.query(BudgetMonth.category_id, func.sum(BudgetMonth.assigned))
                       .filter(BudgetMonth.category_id.in_(cat_ids_all), month_clause)
                       .group_by(BudgetMonth.category_id).all()):
            budget_map[cid] = int(a or 0)

    spent_map = {cid: int(abs(t or 0)) for cid, t in
                 db.session.query(Transaction.category_id, func.sum(Transaction.amount))
                 .filter(Transaction.category_id.in_(cat_ids_all), Transaction.amount < 0,
                         Transaction.date >= start_date, Transaction.date <= end_date,
                         *owner_clauses)
                 .group_by(Transaction.category_id).all()}

    def _pct(spent, budgeted):
        return round(100 * spent / budgeted) if budgeted else None

    groups: dict[str, list] = {}
    for c in categories:
        g = c.group_name or "General"
        groups.setdefault(g, [])
        budgeted = budget_map.get(c.id, 0)
        spent_mo = spent_map.get(c.id, 0)
        groups[g].append({"id": c.id, "name": c.name,
                          "color": _group_color(g),
                          "avg_per_month": avg_map.get(c.id, 0),
                          "last_month": lm_map.get(c.id, 0),
                          "budgeted": budgeted, "spent_month": spent_mo,
                          "spent_pct": _pct(spent_mo, budgeted)})
    legend_groups = []
    for gname, cats in groups.items():
        gb = sum(c["budgeted"] for c in cats)
        gs = sum(c["spent_month"] for c in cats)
        legend_groups.append({"name": gname, "categories": cats,
                              "group_avg": sum(c["avg_per_month"] for c in cats),
                              "group_last_month": sum(c["last_month"] for c in cats),
                              "group_budgeted": gb, "group_spent_month": gs,
                              "group_spent_pct": _pct(gs, gb)})
    return legend_groups


# ---------- routes ----------

def register_routes(app: Flask):

    # ---- dashboard ----
    @app.route("/")
    @setup_required
    @login_required
    def dashboard():
        today = date.today()

        # ---- date-range chart data (same as reports page) ----
        start_date, end_date, preset = _parse_report_range(request)

        cat_totals = (db.session.query(Category.id, Category.name, Category.group_name,
                                       func.sum(Transaction.amount))
                      .join(Transaction, Transaction.category_id == Category.id)
                      .filter(Transaction.date >= start_date,
                              Transaction.date <= end_date,
                              Transaction.amount < 0,
                              Category.is_income.is_(False))
                      .group_by(Category.id)
                      .order_by(Category.group_name, Category.name).all())
        # cat_groups built from stable alphabetical order (drives color assignment)
        _cg: dict = {}
        for r in cat_totals:
            gn = r[2] or "General"
            _cg.setdefault(gn, []).append(r[0])
        cat_groups = [{"name": gn, "cat_ids": ids} for gn, ids in _cg.items()]
        # display arrays sorted largest→smallest for clockwise chart order
        _sorted = sorted(cat_totals, key=lambda r: abs(int(r[3] or 0)), reverse=True)
        cat_ids         = [r[0] for r in _sorted]
        cat_labels      = [r[1] for r in _sorted]
        cat_values      = [abs(int(r[3] or 0)) for r in _sorted]
        cat_group_names = [r[2] or "General" for r in _sorted]

        payee_totals = (db.session.query(Transaction.payee, func.sum(Transaction.amount))
                        .join(Category, Category.id == Transaction.category_id)
                        .filter(Transaction.date >= start_date,
                                Transaction.date <= end_date,
                                Transaction.amount < 0,
                                Transaction.payee != '',
                                Category.is_income.is_(False))
                        .group_by(Transaction.payee)
                        .order_by(func.sum(Transaction.amount))
                        .limit(20).all())
        payee_labels = [r[0] for r in payee_totals]
        payee_values = [abs(int(r[1] or 0)) for r in payee_totals]

        trend_months = []
        cur = date(start_date.year, start_date.month, 1)
        while cur <= end_date:
            trend_months.append((cur.year, cur.month))
            cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
        month_idx    = {ym: i for i, ym in enumerate(trend_months)}
        trend_labels = [f"{y}-{m:02d}" for y, m in trend_months]
        n = len(trend_months)

        monthly_rows = (db.session.query(
                extract('year',  Transaction.date).label('yr'),
                extract('month', Transaction.date).label('mo'),
                Category.id, Category.name, func.sum(Transaction.amount))
            .join(Category, Category.id == Transaction.category_id)
            .filter(Transaction.date >= start_date,
                    Transaction.date <= end_date,
                    Transaction.amount < 0,
                    Category.is_income.is_(False))
            .group_by('yr', 'mo', Category.id).order_by('yr', 'mo').all())
        cat_month_data: dict[int, dict] = {}
        for yr, mo, cid, cname, total in monthly_rows:
            if cid not in cat_month_data:
                cat_month_data[cid] = {"label": cname, "data": [0.0] * n}
            idx = month_idx.get((int(yr), int(mo)))
            if idx is not None:
                cat_month_data[cid]["data"][idx] = round(abs(int(total or 0)) / 100.0, 2)
        trend_datasets = sorted(cat_month_data.values(),
                                key=lambda d: sum(d["data"]), reverse=True)

        # ---- Budget tab data — scoped to the selected range, not hardcoded
        # to "today", so Budgeted and Spent always compare the same period.
        legend_groups = _compute_legend_groups(start_date, end_date)

        # Reference periods for the payee table below (always "this month" /
        # "last calendar month", independent of the selected range).
        lm_year, lm_month = _prev_month(today.year, today.month)
        lm_start = date(lm_year, lm_month, 1)
        lm_end   = date(lm_year, lm_month, monthrange(lm_year, lm_month)[1])
        cm_start = date(today.year, today.month, 1)

        # Payee table rows — parent_id filter keeps split line-items from
        # being counted alongside the parent's already-full amount.
        payee_alltime = (db.session.query(Transaction.payee,
                         func.sum(Transaction.amount), func.min(Transaction.date))
            .filter(Transaction.amount < 0, Transaction.payee != '',
                    Transaction.parent_id.is_(None))
            .group_by(Transaction.payee).all())
        payee_avg_map = {}
        for p, total, first_date_ in payee_alltime:
            if p and first_date_:
                months_ = max(1, (today.year - first_date_.year) * 12
                              + (today.month - first_date_.month) + 1)
                payee_avg_map[p] = int(abs(total or 0) / months_)
        payee_lm_map = {p: int(abs(t or 0)) for p, t in
                        db.session.query(Transaction.payee, func.sum(Transaction.amount))
                        .filter(Transaction.amount < 0, Transaction.payee != '',
                                Transaction.parent_id.is_(None),
                                Transaction.date >= lm_start, Transaction.date <= lm_end)
                        .group_by(Transaction.payee).all() if p}
        payee_cm_map = {p: int(abs(t or 0)) for p, t in
                        db.session.query(Transaction.payee, func.sum(Transaction.amount))
                        .filter(Transaction.amount < 0, Transaction.payee != '',
                                Transaction.parent_id.is_(None),
                                Transaction.date >= cm_start, Transaction.date <= today)
                        .group_by(Transaction.payee).all() if p}
        all_payees = set(payee_avg_map) | set(payee_lm_map) | set(payee_cm_map)
        payee_rows = sorted([
            {"name": p, "avg_per_month": payee_avg_map.get(p, 0),
             "last_month": payee_lm_map.get(p, 0), "spent_month": payee_cm_map.get(p, 0)}
            for p in all_payees if p
        ], key=lambda x: x["avg_per_month"], reverse=True)

        users = User.query.order_by(User.id).all()
        users_json = {str(u.id): u.display_name for u in users}
        month_income   = abs(int(_income_total(today.year, today.month)))
        # Not referenced in home.html (superseded by the JS-side Budget tab),
        # kept only for parity with the existing render_template context.
        month_budgeted = int(db.session.query(func.coalesce(func.sum(BudgetMonth.assigned), 0))
                             .filter(BudgetMonth.year == today.year,
                                     BudgetMonth.month == today.month).scalar() or 0)
        month_spending = int(abs(db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
                             .join(Category, Category.id == Transaction.category_id)
                             .filter(Transaction.date >= cm_start, Transaction.date <= today,
                                     Transaction.amount < 0, Category.is_income.is_(False))
                             .scalar() or 0))

        _ibu_rows = (db.session.query(
                Transaction.owner_user_id,
                func.coalesce(func.sum(Transaction.amount), 0))
            .join(Category, Category.id == Transaction.category_id)
            .filter(extract("year", Transaction.date) == today.year,
                    extract("month", Transaction.date) == today.month,
                    Category.is_income.is_(True))
            .group_by(Transaction.owner_user_id).all())
        income_by_user = {str(r[0]): abs(int(r[1] or 0)) for r in _ibu_rows if r[0] is not None}

        return render_template("home.html",
                               preset=preset,
                               start_date=start_date.isoformat(),
                               end_date=end_date.isoformat(),
                               cat_ids=cat_ids,
                               cat_labels=cat_labels,
                               cat_values=cat_values,
                               cat_group_names=cat_group_names,
                               cat_groups=cat_groups,
                               payee_labels=payee_labels,
                               payee_values=payee_values,
                               trend_labels=trend_labels,
                               trend_datasets=trend_datasets,
                               legend_groups=legend_groups,
                               payee_rows=payee_rows,
                               users=users,
                               users_json=users_json,
                               month_income=month_income,
                               month_spending=month_spending,
                               month_budgeted=month_budgeted,
                               income_by_user=income_by_user)

    # ---- accounts ----
    @app.route("/accounts")
    @login_required
    def accounts_list():
        accounts = Account.query.order_by(Account.closed, Account.name).all()
        return render_template("accounts.html", accounts=accounts)

    @app.route("/accounts/new", methods=["GET", "POST"])
    @login_required
    def account_new():
        if request.method == "POST":
            a = Account(
                name=request.form["name"].strip(),
                type=request.form.get("type", "checking"),
                on_budget=bool(request.form.get("on_budget")),
                starting_balance=to_cents(request.form.get("starting_balance", "0")),
            )
            db.session.add(a)
            db.session.commit()
            flash(f"Created account: {a.name}", "success")
            return redirect(url_for("accounts_list"))
        return render_template("account_form.html", account=None)

    @app.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
    @login_required
    def account_edit(account_id):
        a = db.session.get(Account, account_id) or abort(404)
        if request.method == "POST":
            a.name = request.form["name"].strip()
            a.type = request.form.get("type", a.type)
            a.on_budget = bool(request.form.get("on_budget"))
            a.starting_balance = to_cents(request.form.get("starting_balance", "0"))
            a.closed = bool(request.form.get("closed"))
            db.session.commit()
            flash("Account saved.", "success")
            return redirect(url_for("accounts_list"))
        return render_template("account_form.html", account=a)

    @app.route("/accounts/<int:account_id>/delete", methods=["POST"])
    @login_required
    def account_delete(account_id):
        a = db.session.get(Account, account_id) or abort(404)
        db.session.delete(a)
        db.session.commit()
        flash("Account deleted.", "warning")
        return redirect(url_for("accounts_list"))

    # ---- transactions ----
    @app.route("/transactions")
    @login_required
    def transactions_list():
        account_id  = request.args.get("account_id", type=int)
        initial_cat_id = request.args.get("category_id", type=int)
        initial_payee  = request.args.get("payee", "").strip()
        q = Transaction.query.filter(Transaction.parent_id.is_(None)).options(
            joinedload(Transaction.splits))
        if account_id:
            q = q.filter_by(account_id=account_id)
        txns = q.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(500).all()
        accounts   = Account.query.order_by(Account.name).all()
        categories = Category.query.order_by(Category.group_name, Category.name).all()
        users      = User.query.order_by(User.id).all()
        cat_by_id  = {c.id: c.name for c in categories}
        users_json = {str(u.id): u.display_name for u in users}

        def _hex_rgba(hex_color, alpha):
            h = hex_color.lstrip('#')
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return f"rgba({r},{g},{b},{alpha:.2f})"

        cat_colors, cat_bg_colors, cat_groups_by_id, _gc = {}, {}, {}, {}
        for c in categories:
            g = c.group_name or "General"
            color = _group_color(g)
            cat_colors[c.id] = color
            cat_groups_by_id[c.id] = g
            idx = _gc.get(g, 0)
            cat_bg_colors[c.id] = _hex_rgba(color, min(0.18 + idx * 0.10, 0.45))
            _gc[g] = idx + 1

        return render_template("transactions.html",
                               txns=txns,
                               accounts=accounts,
                               categories=categories,
                               users=users,
                               users_json=users_json,
                               cat_by_id=cat_by_id,
                               cat_colors=cat_colors,
                               cat_bg_colors=cat_bg_colors,
                               cat_groups=cat_groups_by_id,
                               filter_account=account_id,
                               initial_cat_id=initial_cat_id,
                               initial_payee=initial_payee)

    @app.route("/transactions/new", methods=["GET", "POST"])
    @login_required
    def transaction_new():
        accounts = Account.query.filter_by(closed=False).order_by(Account.name).all()
        categories = Category.query.filter_by(hidden=False).order_by(
            Category.group_name, Category.name).all()
        if request.method == "POST":
            kind = request.form.get("kind", "expense")
            account_id = int(request.form["account_id"])
            d = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
            cents = to_cents(request.form.get("amount", "0"))

            if kind == "transfer":
                to_account_id = int(request.form["to_account_id"])
                if to_account_id == account_id:
                    flash("Transfer requires two different accounts.", "danger")
                    return redirect(url_for("transaction_new"))
                tid = str(uuid.uuid4())
                src = db.session.get(Account, account_id)
                dst = db.session.get(Account, to_account_id)
                db.session.add(Transaction(
                    account_id=account_id, date=d, amount=-abs(cents),
                    payee=f"Transfer to {dst.name}",
                    memo=request.form.get("memo", ""),
                    transfer_id=tid, source="manual"))
                db.session.add(Transaction(
                    account_id=to_account_id, date=d, amount=abs(cents),
                    payee=f"Transfer from {src.name}",
                    memo=request.form.get("memo", ""),
                    transfer_id=tid, source="manual"))
            else:
                signed = -abs(cents) if kind == "expense" else abs(cents)
                cat_id = request.form.get("category_id") or None
                db.session.add(Transaction(
                    account_id=account_id,
                    category_id=int(cat_id) if cat_id else None,
                    date=d,
                    amount=signed,
                    payee=request.form.get("payee", "").strip(),
                    memo=request.form.get("memo", "").strip(),
                    cleared=bool(request.form.get("cleared")),
                    source="manual",
                ))
            db.session.commit()
            flash("Transaction added.", "success")
            return redirect(url_for("transactions_list"))
        return render_template("transaction_form.html",
                               accounts=accounts, categories=categories,
                               today=date.today().isoformat())

    @app.route("/transactions/<int:tid>/delete", methods=["POST"])
    @login_required
    def transaction_delete(tid):
        t = db.session.get(Transaction, tid) or abort(404)
        if t.transfer_id:
            partners = Transaction.query.filter_by(transfer_id=t.transfer_id).all()
            for p in partners:
                db.session.delete(p)
        else:
            db.session.delete(t)
        db.session.commit()
        flash("Transaction deleted.", "warning")
        return redirect(url_for("transactions_list"))

    @app.route("/transactions/<int:tid>/categorize", methods=["POST"])
    @login_required
    def transaction_categorize(tid):
        t = db.session.get(Transaction, tid) or abort(404)
        if t.splits:
            return jsonify({"ok": False, "error": "Unsplit this transaction before changing its category."}), 400
        cid = request.form.get("category_id") or None
        t.category_id = int(cid) if cid else None
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/transactions/<int:tid>/edit", methods=["POST"])
    @login_required
    def transaction_edit(tid):
        t = db.session.get(Transaction, tid) or abort(404)
        field = request.form.get("field")
        value = request.form.get("value", "").strip()
        if t.splits and field in ("amount", "category_id"):
            return jsonify({"ok": False, "error": "Unsplit this transaction before changing its amount or category."}), 400
        if field == "date":
            from datetime import date as _date
            try:
                t.date = _date.fromisoformat(value)
            except ValueError:
                return jsonify({"ok": False, "error": "Invalid date"}), 400
        elif field == "account_id":
            t.account_id = int(value)
        elif field == "payee":
            t.payee = value
        elif field == "memo":
            t.memo = value
        elif field == "amount":
            t.amount = to_cents(value)
        elif field == "category_id":
            t.category_id = int(value) if value else None
        elif field == "owner":
            if value == "joint":
                t.is_joint = True
                t.owner_user_id = None
            elif value == "":
                t.is_joint = False
                t.owner_user_id = None
            else:
                try:
                    uid = int(value)
                    if not db.session.get(User, uid):
                        return jsonify({"ok": False, "error": "Unknown user"}), 400
                    t.owner_user_id = uid
                    t.is_joint = False
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "error": "Invalid owner"}), 400
        else:
            return jsonify({"ok": False, "error": "Unknown field"}), 400
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/transactions/<int:tid>/split-info")
    @login_required
    def transaction_split_info(tid):
        """Everything the split modal needs to render, for any transaction on any page."""
        t = db.session.get(Transaction, tid) or abort(404)
        if t.parent_id is not None:
            return jsonify({"error": "This is already a split line-item and can't be split further."}), 400
        cats = (Category.query.filter_by(hidden=False)
                .order_by(Category.group_name, Category.name).all())
        return jsonify({
            "id": t.id,
            "date": t.date.isoformat(),
            "payee": t.payee or "",
            "memo": t.memo or "",
            "amount": t.amount,
            "category_id": t.category_id,
            "is_transfer": bool(t.transfer_id),
            "splits": [{"id": s.id, "category_id": s.category_id, "amount": s.amount,
                        "memo": s.memo or ""} for s in t.splits],
            "categories": [{"id": c.id, "name": c.name, "group": c.group_name} for c in cats],
        })

    @app.route("/transactions/<int:tid>/split", methods=["POST"])
    @login_required
    def transaction_split(tid):
        """Divide a transaction's amount across multiple categories.

        The transaction becomes a "parent" (category cleared, amount kept as
        the total) and each split is created as its own child Transaction row
        so all the existing per-category aggregates just work. Re-posting
        replaces any prior splits wholesale.
        """
        t = db.session.get(Transaction, tid) or abort(404)
        if t.parent_id is not None:
            return jsonify({"ok": False, "error": "This is already a split line-item and can't be split further."}), 400
        if t.transfer_id:
            return jsonify({"ok": False, "error": "Transfers can't be split."}), 400

        payload = request.get_json(silent=True) or {}
        raw_splits = payload.get("splits") or []

        parsed = []
        for row in raw_splits:
            try:
                cat_id = int(row["category_id"]) if row.get("category_id") else None
                cents = to_cents(row.get("amount", "0"))
            except (KeyError, ValueError, TypeError):
                return jsonify({"ok": False, "error": "Invalid split row."}), 400
            if cents == 0:
                continue
            parsed.append((cat_id, cents, (row.get("memo") or "").strip()))

        if len(parsed) < 2:
            return jsonify({"ok": False, "error": "Enter at least two non-zero splits."}), 400
        if sum(cents for _, cents, _ in parsed) != t.amount:
            return jsonify({"ok": False,
                             "error": f"Splits must add up to {fmt_money(t.amount)}."}), 400

        for old in list(t.splits):
            db.session.delete(old)
        db.session.flush()

        for cat_id, cents, memo in parsed:
            db.session.add(Transaction(
                account_id=t.account_id, date=t.date, amount=cents,
                payee=t.payee, memo=memo or t.memo,
                cleared=t.cleared, source="split",
                owner_user_id=t.owner_user_id, is_joint=t.is_joint,
                category_id=cat_id, parent_id=t.id,
            ))
        t.category_id = None
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/transactions/<int:tid>/unsplit", methods=["POST"])
    @login_required
    def transaction_unsplit(tid):
        t = db.session.get(Transaction, tid) or abort(404)
        for old in list(t.splits):
            db.session.delete(old)
        db.session.commit()
        return jsonify({"ok": True})

    # ---- import CSV ----
    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_csv():
        accounts = Account.query.filter_by(closed=False).order_by(Account.name).all()
        if request.method == "POST":
            account_id = int(request.form["account_id"])
            f = request.files.get("file")
            if not f:
                flash("Please choose a CSV file.", "danger")
                return redirect(url_for("import_csv"))

            rows, headers = csv_import.parse_csv(f)
            if not rows:
                flash("No rows could be parsed from that CSV. Check the headers.",
                      "warning")
                return redirect(url_for("import_csv"))

            existing = {x[0] for x in db.session.query(Transaction.external_id)
                        .filter(Transaction.external_id.isnot(None)).all()}
            added = 0
            for r in rows:
                if r["hash"] in existing:
                    continue
                db.session.add(Transaction(
                    account_id=account_id,
                    date=r["date"],
                    amount=r["amount"],
                    payee=r["payee"] or "(imported)",
                    memo=r["memo"],
                    source="csv",
                    external_id=r["hash"],
                ))
                existing.add(r["hash"])
                added += 1
            db.session.commit()
            flash(f"Imported {added} new transactions ({len(rows) - added} duplicates skipped).",
                  "success")
            return redirect(url_for("transactions_list", account_id=account_id))

        return render_template("import.html", accounts=accounts)

    # ---- budget (envelope) ----
    @app.route("/budget")
    @login_required
    def budget_view():
        y = request.args.get("year", type=int) or date.today().year
        m = request.args.get("month", type=int) or date.today().month

        cats = Category.query.filter_by(is_income=False, hidden=False).order_by(
            Category.group_name, Category.name).all()

        cat_ids_all = [c.id for c in cats]
        prev_y, prev_m = _prev_month(y, m)
        lm_start = date(prev_y, prev_m, 1)
        lm_end   = date(y, m, 1) - timedelta(days=1)

        if cat_ids_all:
            _alltime = (db.session.query(
                    Transaction.category_id,
                    func.sum(Transaction.amount),
                    func.min(Transaction.date))
                .filter(Transaction.category_id.in_(cat_ids_all),
                        Transaction.amount < 0)
                .group_by(Transaction.category_id).all())
            avg_map = {}
            for cid, total, first_d in _alltime:
                if first_d:
                    mo = max(1, (y - first_d.year) * 12 + (m - first_d.month) + 1)
                    avg_map[cid] = int(abs(total or 0) / mo)
            lm_map = {cid: int(abs(t or 0)) for cid, t in
                      db.session.query(Transaction.category_id, func.sum(Transaction.amount))
                      .filter(Transaction.category_id.in_(cat_ids_all),
                              Transaction.amount < 0,
                              Transaction.date >= lm_start,
                              Transaction.date <= lm_end)
                      .group_by(Transaction.category_id).all()}
        else:
            avg_map = lm_map = {}

        rows_by_group = defaultdict(list)
        total_assigned = 0
        total_spent = 0
        total_available = 0
        for c in cats:
            assigned = _assigned_for(y, m, c.id)
            spent = _spent_for(y, m, c.id)
            carryover = _carryover_for(y, m, c.id)
            available = carryover + assigned + spent
            total_assigned += assigned
            total_spent += spent
            total_available += available
            rows_by_group[c.group_name].append({
                "cat": c,
                "assigned": assigned,
                "spent": spent,
                "available": available,
                "carryover": carryover,
                "avg": avg_map.get(c.id, 0),
                "last_month": lm_map.get(c.id, 0),
            })

        income_this_month = _income_total(y, m)
        prior_surplus = _prior_surplus(y, m)
        to_be_budgeted = prior_surplus + income_this_month - total_assigned

        return render_template("budget.html",
                               year=y, month=m,
                               rows_by_group=rows_by_group,
                               total_assigned=total_assigned,
                               total_spent=total_spent,
                               total_available=total_available,
                               income_this_month=income_this_month,
                               prior_surplus=prior_surplus,
                               to_be_budgeted=to_be_budgeted,
                               prev_month=_prev_month(y, m),
                               next_month=_next_month(y, m))

    @app.route("/budget/assign", methods=["POST"])
    @login_required
    def budget_assign():
        y = int(request.form["year"])
        m = int(request.form["month"])
        cid = int(request.form["category_id"])
        cents = to_cents(request.form.get("amount", "0"))
        bm = BudgetMonth.query.filter_by(year=y, month=m, category_id=cid).first()
        if not bm:
            bm = BudgetMonth(year=y, month=m, category_id=cid, assigned=cents)
            db.session.add(bm)
        else:
            bm.assigned = cents
        db.session.commit()
        return jsonify({"ok": True, "assigned": cents})

    # ---- categories ----
    @app.route("/categories", methods=["GET", "POST"])
    @login_required
    def categories_list():
        if request.method == "POST":
            name = request.form["name"].strip()
            group = request.form.get("group_name", "General").strip() or "General"
            is_income = bool(request.form.get("is_income"))
            if name:
                exists = Category.query.filter_by(name=name, group_name=group).first()
                if not exists:
                    db.session.add(Category(name=name, group_name=group,
                                            is_income=is_income))
                    db.session.commit()
                    flash(f"Added category: {group} / {name}", "success")
            return redirect(url_for("categories_list"))
        cats = Category.query.order_by(Category.group_name, Category.name).all()
        groups = defaultdict(list)
        for c in cats:
            groups[c.group_name].append(c)
        return render_template("categories.html", groups=groups)

    @app.route("/categories/<int:cid>/edit", methods=["POST"])
    @login_required
    def category_edit(cid):
        c = db.session.get(Category, cid) or abort(404)
        field = request.form.get("field")
        value = request.form.get("value", "").strip()
        if field == "name" and value:
            c.name = value
        elif field == "group_name" and value:
            c.group_name = value
        elif field == "is_income":
            c.is_income = value == "1"
        else:
            return jsonify({"ok": False, "error": "invalid field"}), 400
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/categories/group/rename", methods=["POST"])
    @login_required
    def category_group_rename():
        old = request.form.get("old_name", "").strip()
        new = request.form.get("new_name", "").strip()
        if not old or not new or old == new:
            return jsonify({"ok": False, "error": "invalid names"}), 400
        Category.query.filter_by(group_name=old).update({"group_name": new})
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/categories/<int:cid>/delete", methods=["POST"])
    @login_required
    def category_delete(cid):
        c = db.session.get(Category, cid) or abort(404)
        Transaction.query.filter_by(category_id=cid).update({"category_id": None})
        BudgetMonth.query.filter_by(category_id=cid).delete()
        db.session.delete(c)
        db.session.commit()
        flash("Category deleted.", "warning")
        return redirect(url_for("categories_list"))

    # ---- reports (now merged into home) ----
    @app.route("/reports")
    @login_required
    def reports():
        qs = request.query_string.decode()
        return redirect(url_for("dashboard") + (f"?{qs}" if qs else ""))

    @app.route("/reports/category-transactions")
    @login_required
    def reports_category_transactions():
        try:
            cat_id = int(request.args["category_id"])
            start_date = datetime.strptime(request.args["start_date"], "%Y-%m-%d").date()
            end_date   = datetime.strptime(request.args["end_date"],   "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return jsonify({"error": "invalid params"}), 400

        cat = Category.query.get_or_404(cat_id)
        txns = (Transaction.query
                .filter(Transaction.category_id == cat_id,
                        Transaction.date >= start_date,
                        Transaction.date <= end_date)
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .all())
        rows = [{"id": t.id,
                 "date": t.date.isoformat(),
                 "payee": t.payee or "",
                 "memo": t.memo or "",
                 "amount": t.amount,
                 "account": t.account.name if t.account else "",
                 "category_id": t.category_id or "",
                 "category_name": t.category.name if t.category else "",
                 "parent_id": t.parent_id} for t in txns]
        total    = sum(t.amount for t in txns)
        expenses = sum(abs(t.amount) for t in txns if t.amount < 0)
        all_cats = (Category.query
                    .filter_by(hidden=False)
                    .order_by(Category.group_name, Category.sort_order, Category.name)
                    .all())
        cat_list = [{"id": c.id, "name": c.name, "group": c.group_name} for c in all_cats]
        return jsonify({"name": cat.name, "transactions": rows, "total": total,
                        "expenses": expenses, "categories": cat_list})

    @app.route("/reports/payee-transactions")
    @login_required
    def reports_payee_transactions():
        try:
            payee      = request.args["payee"]
            start_date = datetime.strptime(request.args["start_date"], "%Y-%m-%d").date()
            end_date   = datetime.strptime(request.args["end_date"],   "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return jsonify({"error": "invalid params"}), 400

        txns = (Transaction.query
                .filter(Transaction.payee == payee,
                        Transaction.parent_id.is_(None),
                        Transaction.date >= start_date,
                        Transaction.date <= end_date)
                .options(joinedload(Transaction.splits))
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .all())
        rows = [{"id": t.id,
                 "date": t.date.isoformat(),
                 "payee": t.payee or "",
                 "memo": t.memo or "",
                 "amount": t.amount,
                 "account": t.account.name if t.account else "",
                 "category_id": t.category_id or "",
                 "category_name": ("Split (%d)" % len(t.splits)) if t.splits
                                   else (t.category.name if t.category else ""),
                 "parent_id": t.parent_id} for t in txns]
        expenses = sum(abs(t.amount) for t in txns if t.amount < 0)
        all_cats = (Category.query
                    .filter_by(hidden=False)
                    .order_by(Category.group_name, Category.sort_order, Category.name)
                    .all())
        cat_list = [{"id": c.id, "name": c.name, "group": c.group_name} for c in all_cats]
        return jsonify({"payee": payee, "transactions": rows,
                        "expenses": expenses, "categories": cat_list})

    @app.route("/reports/filtered-transactions")
    @login_required
    def reports_filtered_transactions():
        """Transactions matching the intersection of category + payee + owner filters."""
        try:
            sd = datetime.strptime(request.args["start_date"], "%Y-%m-%d").date()
            ed = datetime.strptime(request.args["end_date"],   "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return jsonify({"error": "invalid dates"}), 400

        owner_clauses  = _owner_filter_clauses(request.args.get("owner", "all"))
        filter_cat_ids = [int(x) for x in request.args.getlist("category_ids") if x.isdigit()]
        filter_payees  = [x for x in request.args.getlist("payees") if x]

        q = (Transaction.query
             .join(Category, Category.id == Transaction.category_id)
             .filter(Transaction.date >= sd, Transaction.date <= ed,
                     Transaction.amount < 0, Category.is_income.is_(False),
                     *owner_clauses))
        if filter_cat_ids:
            q = q.filter(Transaction.category_id.in_(filter_cat_ids))
        if filter_payees:
            q = q.filter(Transaction.payee.in_(filter_payees))

        txns = q.order_by(Transaction.date.desc(), Transaction.id.desc()).all()
        all_cats = (Category.query.filter_by(hidden=False)
                    .order_by(Category.group_name, Category.sort_order, Category.name).all())
        cat_list = [{"id": c.id, "name": c.name, "group": c.group_name} for c in all_cats]
        rows = [{"id": t.id, "date": t.date.isoformat(),
                 "payee": t.payee or "", "memo": t.memo or "", "amount": t.amount,
                 "account": t.account.name if t.account else "",
                 "category_id": t.category_id or "",
                 "category_name": t.category.name if t.category else "",
                 "group_name": t.category.group_name if t.category else "",
                 "is_joint": t.is_joint,
                 "owner_user_id": t.owner_user_id,
                 "owner_name": t.owner.display_name if t.owner else None,
                 "parent_id": t.parent_id} for t in txns]
        expenses = sum(abs(t.amount) for t in txns)
        return jsonify({"transactions": rows, "expenses": expenses, "categories": cat_list})

    # ---- goals ----
    @app.route("/goals", methods=["GET", "POST"])
    @login_required
    def goals_list():
        if request.method == "POST":
            g = Goal(
                name=request.form["name"].strip(),
                target_amount=to_cents(request.form.get("target_amount", "0")),
                target_date=(datetime.strptime(request.form["target_date"], "%Y-%m-%d").date()
                             if request.form.get("target_date") else None),
                category_id=int(request.form["category_id"]) if request.form.get("category_id") else None,
                note=request.form.get("note", "").strip(),
            )
            db.session.add(g)
            db.session.commit()
            flash(f"Goal created: {g.name}", "success")
            return redirect(url_for("goals_list"))
        goals = Goal.query.order_by(Goal.completed, Goal.target_date.is_(None),
                                     Goal.target_date).all()
        categories = Category.query.filter_by(is_income=False, hidden=False).order_by(
            Category.group_name, Category.name).all()
        return render_template("goals.html", goals=goals, categories=categories)

    @app.route("/goals/<int:gid>/contribute", methods=["POST"])
    @login_required
    def goal_contribute(gid):
        g = db.session.get(Goal, gid) or abort(404)
        cents = to_cents(request.form.get("amount", "0"))
        g.saved_amount = (g.saved_amount or 0) + cents
        if g.saved_amount >= g.target_amount and g.target_amount > 0:
            g.completed = True
        db.session.commit()
        flash("Contribution logged.", "success")
        return redirect(url_for("goals_list"))

    @app.route("/goals/<int:gid>/delete", methods=["POST"])
    @login_required
    def goal_delete(gid):
        g = db.session.get(Goal, gid) or abort(404)
        db.session.delete(g)
        db.session.commit()
        flash("Goal deleted.", "warning")
        return redirect(url_for("goals_list"))

    # ---- recurring ----
    @app.route("/recurring", methods=["GET", "POST"])
    @login_required
    def recurring_list():
        accounts = Account.query.filter_by(closed=False).order_by(Account.name).all()
        categories = Category.query.filter_by(hidden=False).order_by(
            Category.group_name, Category.name).all()
        if request.method == "POST":
            kind = request.form.get("kind", "expense")
            cents = to_cents(request.form.get("amount", "0"))
            signed = -abs(cents) if kind == "expense" else abs(cents)
            r = Recurring(
                account_id=int(request.form["account_id"]),
                category_id=int(request.form["category_id"]) if request.form.get("category_id") else None,
                amount=signed,
                payee=request.form.get("payee", "").strip(),
                memo=request.form.get("memo", "").strip(),
                frequency=request.form.get("frequency", "monthly"),
                interval=int(request.form.get("interval", "1") or 1),
                next_date=datetime.strptime(request.form["next_date"], "%Y-%m-%d").date(),
            )
            db.session.add(r)
            db.session.commit()
            post_due_recurring()
            flash(f"Scheduled recurring transaction: {r.payee or '(unnamed)'}", "success")
            return redirect(url_for("recurring_list"))
        items = Recurring.query.order_by(Recurring.active.desc(),
                                          Recurring.next_date).all()
        return render_template("recurring.html",
                               items=items,
                               accounts=accounts,
                               categories=categories,
                               today=date.today().isoformat())

    @app.route("/recurring/<int:rid>/toggle", methods=["POST"])
    @login_required
    def recurring_toggle(rid):
        r = db.session.get(Recurring, rid) or abort(404)
        r.active = not r.active
        db.session.commit()
        return redirect(url_for("recurring_list"))

    @app.route("/recurring/<int:rid>/delete", methods=["POST"])
    @login_required
    def recurring_delete(rid):
        r = db.session.get(Recurring, rid) or abort(404)
        db.session.delete(r)
        db.session.commit()
        flash("Recurring transaction deleted.", "warning")
        return redirect(url_for("recurring_list"))

    # ---- plaid ----
    @app.route("/plaid")
    @login_required
    def plaid_status():
        items = PlaidItem.query.all()
        return render_template("plaid.html",
                               configured=plaid_client.is_configured(),
                               env=plaid_client.PLAID_ENV,
                               items=items)

    @app.route("/plaid/link_token", methods=["POST"])
    @login_required
    def plaid_link_token():
        if not plaid_client.is_configured():
            return jsonify({"error": "PLAID_CLIENT_ID and PLAID_SECRET not set"}), 400
        try:
            token = plaid_client.create_link_token()
            return jsonify({"link_token": token})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/plaid/exchange", methods=["POST"])
    @login_required
    def plaid_exchange():
        if not plaid_client.is_configured():
            return jsonify({"error": "Plaid not configured"}), 400
        public_token = request.json.get("public_token")
        try:
            access, item_id = plaid_client.exchange_public_token(public_token)
            inst = request.json.get("institution_name", "")
            item = PlaidItem(item_id=item_id, access_token=access,
                              institution_name=inst)
            db.session.add(item)
            accts = plaid_client.get_accounts(access)
            for a in accts:
                existing = Account.query.filter_by(plaid_account_id=a["account_id"]).first()
                if existing:
                    continue
                acc_type = "credit" if "credit" in a["type"] else (
                    "savings" if a["subtype"] == "savings" else "checking")
                db.session.add(Account(
                    name=f"{inst} – {a['name']}" if inst else a["name"],
                    type=acc_type,
                    on_budget=True,
                    starting_balance=int(round((a["balance"] or 0) * 100)),
                    plaid_account_id=a["account_id"],
                    plaid_item_id=item_id,
                ))
            db.session.commit()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/plaid/sync/<int:item_id>", methods=["POST"])
    @login_required
    def plaid_sync(item_id):
        item = db.session.get(PlaidItem, item_id) or abort(404)
        try:
            result = plaid_client.sync_transactions(item.access_token, item.cursor)
            account_map = {a.plaid_account_id: a.id for a in
                            Account.query.filter(Account.plaid_item_id == item.item_id)}
            added = 0
            for t in result["added"]:
                acct_local = account_map.get(t["account_id"])
                if not acct_local:
                    continue
                if Transaction.query.filter_by(external_id=t["transaction_id"]).first():
                    continue
                cents = -int(round(float(t["amount"]) * 100))
                db.session.add(Transaction(
                    account_id=acct_local,
                    date=t["date"],
                    amount=cents,
                    payee=t.get("name", "") or t.get("merchant_name", ""),
                    memo="",
                    source="plaid",
                    external_id=t["transaction_id"],
                ))
                added += 1
            item.cursor = result["next_cursor"]
            db.session.commit()
            flash(f"Synced {added} new transactions from Plaid.", "success")
        except Exception as e:
            flash(f"Plaid sync error: {e}", "danger")
        return redirect(url_for("plaid_status"))

    # ---- calendar API ----

    _GOOGLE_CALENDAR_ICS = os.environ.get("GOOGLE_CALENDAR_ICS", "")

    # Same palette Chart.js uses for its doughnut chart by default.
    _CHART_PALETTE = [
        "#ff6384", "#ff9f40", "#ffcd56", "#4bc0c0",
        "#36a2eb", "#9966ff", "#c9cbcf",
    ]

    @app.route("/api/calendar/transactions")
    @login_required
    def api_calendar_transactions():
        """Return budget transactions as FullCalendar-compatible JSON events.

        color_by=type (default): green for income, red for expense.
        color_by=category: each category gets a stable color from the Chart.js palette.
        """
        start = request.args.get("start")
        end = request.args.get("end")
        color_by = request.args.get("color_by", "type")

        q = Transaction.query
        if start:
            q = q.filter(Transaction.date >= start[:10])
        if end:
            q = q.filter(Transaction.date <= end[:10])

        events = []
        for t in q.order_by(Transaction.date).all():
            label = t.payee or (t.category.name if t.category else "Transaction")

            if color_by == "category":
                if t.category and t.category.is_income:
                    color = "#198754"
                elif t.category:
                    color = _group_color(t.category.group_name or "General")
                else:
                    color = "#6c757d"
            else:
                color = "#198754" if t.amount >= 0 else "#dc3545"

            events.append({
                "id": f"txn-{t.id}",
                "title": f"{label} {fmt_money(t.amount)}",
                "start": t.date.isoformat(),
                "allDay": True,
                "color": color,
                "extendedProps": {
                    "category": t.category.name if t.category else "Uncategorized",
                    "amount": t.amount,
                },
                "url": url_for("transactions_list", category_id=t.category_id)
                       if t.category_id else url_for("transactions_list"),
            })
        return jsonify(events)

    @app.route("/calendar")
    @login_required
    def calendar_view():
        return redirect(url_for("dashboard"))

    @app.route("/calendar-old")
    @login_required
    def calendar_view_old():
        """Kept for reference — calendar is now merged into home."""
        today = date.today()
        lm_year, lm_month = _prev_month(today.year, today.month)
        lm_start = date(lm_year, lm_month, 1)
        lm_end   = date(lm_year, lm_month, monthrange(lm_year, lm_month)[1])

        categories = (Category.query
                      .filter_by(is_income=False, hidden=False)
                      .order_by(Category.group_name, Category.sort_order, Category.name)
                      .all())

        cat_ids = [c.id for c in categories]
        if not cat_ids:
            return render_template("calendar.html", legend=[])

        # All-time totals + earliest date → average monthly spend
        alltime_rows = (db.session.query(
                Transaction.category_id,
                func.sum(Transaction.amount),
                func.min(Transaction.date))
            .filter(Transaction.category_id.in_(cat_ids), Transaction.amount < 0)
            .group_by(Transaction.category_id)
            .all())

        avg_map = {}
        for cat_id, total, first_date in alltime_rows:
            if first_date:
                months = max(1, (today.year - first_date.year) * 12
                             + (today.month - first_date.month) + 1)
                avg_map[cat_id] = int(abs(total or 0) / months)

        # Last-month totals
        lm_rows = (db.session.query(
                Transaction.category_id,
                func.sum(Transaction.amount))
            .filter(Transaction.category_id.in_(cat_ids),
                    Transaction.amount < 0,
                    Transaction.date >= lm_start,
                    Transaction.date <= lm_end)
            .group_by(Transaction.category_id)
            .all())
        lm_map = {cat_id: int(abs(total or 0)) for cat_id, total in lm_rows}

        # This-month budgeted (BudgetMonth.assigned)
        budget_rows = (db.session.query(
                BudgetMonth.category_id,
                BudgetMonth.assigned)
            .filter(BudgetMonth.year == today.year,
                    BudgetMonth.month == today.month,
                    BudgetMonth.category_id.in_(cat_ids))
            .all())
        budget_map = {cat_id: int(assigned or 0) for cat_id, assigned in budget_rows}

        # This-month spent
        cm_start = date(today.year, today.month, 1)
        cm_rows = (db.session.query(
                Transaction.category_id,
                func.sum(Transaction.amount))
            .filter(Transaction.category_id.in_(cat_ids),
                    Transaction.amount < 0,
                    Transaction.date >= cm_start,
                    Transaction.date <= today)
            .group_by(Transaction.category_id)
            .all())
        cm_map = {cat_id: int(abs(total or 0)) for cat_id, total in cm_rows}

        def _pct(spent, budgeted):
            return round(100 * spent / budgeted) if budgeted else None

        # Group categories preserving query order
        groups: dict[str, list] = {}
        for c in categories:
            g = c.group_name or "General"
            if g not in groups:
                groups[g] = []
            budgeted = budget_map.get(c.id, 0)
            spent_mo = cm_map.get(c.id, 0)
            groups[g].append({
                "id": c.id,
                "name": c.name,
                "color": _group_color(g),
                "avg_per_month": avg_map.get(c.id, 0),
                "last_month": lm_map.get(c.id, 0),
                "budgeted": budgeted,
                "spent_month": spent_mo,
                "spent_pct": _pct(spent_mo, budgeted),
            })

        legend_groups = []
        for group_name, cats in groups.items():
            g_budgeted = sum(c["budgeted"] for c in cats)
            g_spent    = sum(c["spent_month"] for c in cats)
            legend_groups.append({
                "name": group_name,
                "categories": cats,
                "group_avg": sum(c["avg_per_month"] for c in cats),
                "group_last_month": sum(c["last_month"] for c in cats),
                "group_budgeted": g_budgeted,
                "group_spent_month": g_spent,
                "group_spent_pct": _pct(g_spent, g_budgeted),
            })
        # ---- payee stats ----
        payee_alltime = (db.session.query(
                Transaction.payee,
                func.sum(Transaction.amount),
                func.min(Transaction.date))
            .filter(Transaction.amount < 0, Transaction.payee != '')
            .group_by(Transaction.payee)
            .all())

        payee_avg_map = {}
        for payee, total, first_date in payee_alltime:
            if payee and first_date:
                months = max(1, (today.year - first_date.year) * 12
                             + (today.month - first_date.month) + 1)
                payee_avg_map[payee] = int(abs(total or 0) / months)

        payee_lm_rows = (db.session.query(
                Transaction.payee, func.sum(Transaction.amount))
            .filter(Transaction.amount < 0, Transaction.payee != '',
                    Transaction.date >= lm_start, Transaction.date <= lm_end)
            .group_by(Transaction.payee).all())
        payee_lm_map = {p: int(abs(t or 0)) for p, t in payee_lm_rows if p}

        payee_cm_rows = (db.session.query(
                Transaction.payee, func.sum(Transaction.amount))
            .filter(Transaction.amount < 0, Transaction.payee != '',
                    Transaction.date >= cm_start, Transaction.date <= today)
            .group_by(Transaction.payee).all())
        payee_cm_map = {p: int(abs(t or 0)) for p, t in payee_cm_rows if p}

        all_payees = set(payee_avg_map) | set(payee_lm_map) | set(payee_cm_map)
        payee_rows = sorted([
            {
                "name": p,
                "avg_per_month": payee_avg_map.get(p, 0),
                "last_month":    payee_lm_map.get(p, 0),
                "spent_month":   payee_cm_map.get(p, 0),
            }
            for p in all_payees if p
        ], key=lambda x: x["avg_per_month"], reverse=True)

        return render_template("calendar.html", legend_groups=legend_groups,
                               payee_rows=payee_rows)

    @app.route("/api/date-range")
    @login_required
    def api_date_range():
        """Return computed start/end dates for a given preset (or custom range)."""
        start, end, preset = _parse_report_range(request)
        return jsonify({"start_date": start.isoformat(), "end_date": end.isoformat(), "preset": preset})

    @app.route("/api/dashboard-chart-data")
    @login_required
    def api_dashboard_chart_data():
        """Return category + payee aggregates for the spending donut charts.

        Accepts start_date, end_date, owner (all | joint | <user_id>).
        Cross-filter: repeated category_ids filters the payee chart;
        repeated payees filters the category chart.
        """
        try:
            sd = datetime.strptime(request.args["start_date"], "%Y-%m-%d").date()
            ed = datetime.strptime(request.args["end_date"],   "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return jsonify({"error": "invalid dates"}), 400

        owner_clauses  = _owner_filter_clauses(request.args.get("owner", "all"))
        filter_cat_ids = [int(x) for x in request.args.getlist("category_ids") if x.isdigit()]
        filter_payees  = [x for x in request.args.getlist("payees") if x]

        # Category aggregates — cross-filtered by selected payees
        cat_q = (db.session.query(Category.id, Category.name, Category.group_name,
                                  func.sum(Transaction.amount))
                 .join(Transaction, Transaction.category_id == Category.id)
                 .filter(Transaction.date >= sd, Transaction.date <= ed,
                         Transaction.amount < 0, Category.is_income.is_(False),
                         *owner_clauses))
        if filter_payees:
            cat_q = cat_q.filter(Transaction.payee.in_(filter_payees))
        cat_totals = (cat_q.group_by(Category.id)
                      .order_by(Category.group_name, Category.name).all())

        # stable group order (alphabetical) for color assignment
        _cg: dict = {}
        for r in cat_totals:
            gn = r[2] or "General"
            _cg.setdefault(gn, []).append(r[0])
        # display arrays sorted largest→smallest
        _sorted = sorted(cat_totals, key=lambda r: abs(int(r[3] or 0)), reverse=True)

        # Payee aggregates — cross-filtered by selected categories
        payee_q = (db.session.query(Transaction.payee, func.sum(Transaction.amount))
                   .join(Category, Category.id == Transaction.category_id)
                   .filter(Transaction.date >= sd, Transaction.date <= ed,
                           Transaction.amount < 0, Transaction.payee != "",
                           Category.is_income.is_(False), *owner_clauses))
        if filter_cat_ids:
            payee_q = payee_q.filter(Transaction.category_id.in_(filter_cat_ids))
        payee_totals = (payee_q.group_by(Transaction.payee)
                        .order_by(func.sum(Transaction.amount)).limit(20).all())

        income_total = abs(int(db.session.query(
                func.coalesce(func.sum(Transaction.amount), 0))
            .join(Category, Category.id == Transaction.category_id)
            .filter(Transaction.date >= sd, Transaction.date <= ed,
                    Category.is_income.is_(True), *owner_clauses)
            .scalar() or 0))

        _ibu = (db.session.query(
                Transaction.owner_user_id,
                func.coalesce(func.sum(Transaction.amount), 0))
            .join(Category, Category.id == Transaction.category_id)
            .filter(Transaction.date >= sd, Transaction.date <= ed,
                    Category.is_income.is_(True), *owner_clauses)
            .group_by(Transaction.owner_user_id).all())
        income_by_user_api = {str(r[0]): abs(int(r[1] or 0)) for r in _ibu if r[0] is not None}

        # Budget tab data for this same range, so it updates in lockstep with
        # everything else when Range/Who changes instead of staying pinned
        # to whatever month the page happened to load on.
        budget_groups = _compute_legend_groups(sd, ed, owner_clauses)

        return jsonify({
            "cat": {
                "ids":         [r[0] for r in _sorted],
                "labels":      [r[1] for r in _sorted],
                "group_names": [r[2] or "General" for r in _sorted],
                "values":      [abs(int(r[3] or 0)) for r in _sorted],
                "groups":      [{"name": gn, "cat_ids": ids} for gn, ids in _cg.items()],
            },
            "payee": {
                "labels": [r[0] for r in payee_totals],
                "values": [abs(int(r[1] or 0)) for r in payee_totals],
            },
            "budget": {
                "groups": budget_groups,
            },
            "income": income_total,
            "income_by_user": income_by_user_api,
        })

    @app.route("/api/category/recent-transactions")
    @login_required
    def api_category_recent_transactions():
        try:
            cat_id = int(request.args["category_id"])
        except (KeyError, ValueError):
            return jsonify({"error": "invalid params"}), 400
        limit = min(int(request.args.get("limit", 8)), 20)
        txns = (Transaction.query
                .filter_by(category_id=cat_id)
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .limit(limit).all())
        return jsonify({"transactions": [
            {"date": t.date.isoformat(), "payee": t.payee or "", "memo": t.memo or "", "amount": t.amount}
            for t in txns
        ]})

    @app.route("/api/payee/recent-transactions")
    @login_required
    def api_payee_recent_transactions():
        payee = request.args.get("payee", "")
        limit = min(int(request.args.get("limit", 8)), 20)
        txns = (Transaction.query
                .filter(Transaction.payee == payee)
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .limit(limit).all())
        return jsonify({"transactions": [
            {"date": t.date.isoformat(), "payee": t.payee or "",
             "memo": t.memo or "", "amount": t.amount}
            for t in txns
        ]})

    @app.route("/api/calendar/google-proxy")
    @login_required
    def api_calendar_google_proxy():
        """Proxy the public Google Calendar ICS feed to avoid browser CORS issues."""
        import urllib.request as _ur
        try:
            with _ur.urlopen(_GOOGLE_CALENDAR_ICS, timeout=10) as resp:
                data = resp.read()
            return data, 200, {"Content-Type": "text/calendar; charset=utf-8",
                               "Cache-Control": "no-cache"}
        except Exception:
            return "", 502

    # ---- AI analysis ----

    @app.route("/ai")
    @setup_required
    @login_required
    def ai_analysis():
        pending_payees = AiPayeeSuggestion.query.filter_by(status="pending").order_by(
            AiPayeeSuggestion.created_at.desc()
        ).all()
        pending_categories = (
            AiCategorySuggestion.query
            .join(Transaction, AiCategorySuggestion.transaction_id == Transaction.id)
            .filter(AiCategorySuggestion.status == "pending")
            .options(joinedload(AiCategorySuggestion.transaction),
                    joinedload(AiCategorySuggestion.suggested_category))
            .order_by(AiCategorySuggestion.created_at.desc())
            .all()
        )
        categories = (Category.query.filter_by(hidden=False)
                     .order_by(Category.group_name, Category.name).all())
        return render_template("ai.html",
                               pending_suggestions=pending_payees,
                               pending_count=len(pending_payees),
                               pending_categories=pending_categories,
                               pending_category_count=len(pending_categories),
                               categories=categories)

    @app.route("/api/ai/chat", methods=["POST"])
    @login_required
    def api_ai_chat():
        data = request.get_json(silent=True) or {}
        messages = data.get("messages", [])
        if not messages:
            return jsonify({"error": "no messages"}), 400

        # Build budget context before entering the generator so SQLAlchemy
        # objects are fully loaded while the request context is still warm.
        budget_context = _build_budget_context()
        system_prompt = (
            "You are a helpful financial advisor for a household budget. "
            "Analyze the data below and help the household understand their spending, "
            "find opportunities to cut expenses, and improve their financial health. "
            "Be specific, concise, and actionable. Use dollar amounts from the data "
            "to support your observations.\n\n"
            "Notes on the data:\n"
            "- Negative amounts = spending/outflows; positive = income/inflows\n"
            "- Amounts are in USD\n\n"
            f"{budget_context}"
        )

        def generate():
            try:
                client = _get_ai_client()
                with client.messages.stream(
                    model="claude-opus-4-8",
                    max_tokens=4096,
                    system=system_prompt,
                    messages=messages,
                    thinking={"type": "adaptive"},
                ) as stream:
                    for chunk in stream.text_stream:
                        yield f"data: {json.dumps({'text': chunk})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/ai/suggest-payees", methods=["POST"])
    @login_required
    def api_ai_suggest_payees():
        ninety_days_ago = date.today() - timedelta(days=90)
        payee_rows = (
            db.session.query(
                Transaction.payee,
                func.count().label("cnt"),
                func.sum(Transaction.amount).label("tot"),
            )
            .filter(Transaction.date >= ninety_days_ago, Transaction.payee != "")
            .group_by(Transaction.payee)
            .having(func.count() >= 1)
            .order_by(func.sum(Transaction.amount))
            .all()
        )

        if not payee_rows:
            return jsonify({"suggestions": [], "message": "No payees found in the last 90 days."})

        payee_list = "\n".join(
            f"{row.payee} ({row.cnt} transactions, ${abs(int(row.tot or 0))/100:,.2f})"
            for row in payee_rows
            if row.payee
        )

        prompt = (
            "Analyze these payee names from bank/credit card transactions and identify ones "
            "that should be normalized to a cleaner, human-readable name.\n\n"
            "Look for:\n"
            "1. Raw merchant codes (e.g. 'AMZN MKTP US*AB12CD3' → 'Amazon')\n"
            "2. The same merchant with slightly different names across transactions\n"
            "3. Truncated or abbreviated names that can be expanded\n\n"
            f"Payee list:\n{payee_list}\n\n"
            "Call the submit_payee_suggestions tool with your suggestions. "
            "Only include suggestions where the improvement is clear. "
            "Omit payees that are already clean and readable. "
            "Pass an empty array if no changes are needed."
        )

        try:
            parsed = _ai_tool_call(
                prompt,
                tool_name="submit_payee_suggestions",
                tool_description="Submit the list of payee name normalization suggestions.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "suggestions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "original": {"type": "string", "description": "Exact original payee name as shown in the list."},
                                    "canonical": {"type": "string", "description": "Clean, human-readable name."},
                                    "reason": {"type": "string", "description": "Brief explanation of the change."},
                                },
                                "required": ["original", "canonical", "reason"],
                            },
                        },
                    },
                    "required": ["suggestions"],
                },
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        suggestions = parsed.get("suggestions", [])

        # Replace existing pending suggestions
        AiPayeeSuggestion.query.filter_by(status="pending").delete()

        created = []
        for s in suggestions:
            original = (s.get("original") or "").strip()
            canonical = (s.get("canonical") or "").strip()
            reason = (s.get("reason") or "").strip()
            if not original or not canonical or original == canonical:
                continue
            count = Transaction.query.filter_by(payee=original).count()
            if count == 0:
                continue
            db.session.add(AiPayeeSuggestion(
                original_payee=original,
                suggested_payee=canonical,
                reason=reason,
                transaction_count=count,
                status="pending",
            ))
            created.append({
                "original": original,
                "canonical": canonical,
                "reason": reason,
                "count": count,
            })

        db.session.commit()
        return jsonify({"suggestions": created, "count": len(created)})

    @app.route("/api/ai/approve-payee/<int:sid>", methods=["POST"])
    @login_required
    def api_ai_approve_payee(sid):
        suggestion = AiPayeeSuggestion.query.get_or_404(sid)
        if suggestion.status != "pending":
            return jsonify({"error": "already processed"}), 400
        updated = (Transaction.query
                   .filter_by(payee=suggestion.original_payee)
                   .update({"payee": suggestion.suggested_payee}))
        suggestion.status = "approved"
        db.session.commit()
        return jsonify({"ok": True, "updated_transactions": updated})

    @app.route("/api/ai/reject-payee/<int:sid>", methods=["POST"])
    @login_required
    def api_ai_reject_payee(sid):
        suggestion = AiPayeeSuggestion.query.get_or_404(sid)
        if suggestion.status != "pending":
            return jsonify({"error": "already processed"}), 400
        suggestion.status = "rejected"
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/ai/suggest-categories", methods=["POST"])
    @login_required
    def api_ai_suggest_categories():
        # Only transactions that are actually eligible for a direct category:
        # not a transfer (never categorized), not a split parent (splits carry
        # the category on their line-items instead).
        candidates = (
            Transaction.query
            .filter(Transaction.category_id.is_(None),
                   Transaction.parent_id.is_(None),
                   Transaction.transfer_id.is_(None))
            .filter(~Transaction.splits.any())
            .order_by(Transaction.date.desc())
            .limit(150)
            .all()
        )

        if not candidates:
            return jsonify({"suggestions": [], "message": "No uncategorized transactions found."})

        categories = (Category.query.filter_by(hidden=False)
                     .order_by(Category.group_name, Category.name).all())
        cat_lookup = {(c.group_name.strip().lower(), c.name.strip().lower()): c
                      for c in categories}
        cat_list_text = "\n".join(
            f"  {c.group_name} > {c.name}" + (" (income)" if c.is_income else "")
            for c in categories
        )

        # Give Claude a payee → category cheat-sheet from transactions that
        # are already categorized, so it can pattern-match recurring payees
        # instead of guessing from the name alone every time.
        payees_needed = {t.payee for t in candidates if t.payee}
        history_rows = (
            db.session.query(Transaction.payee, Category.group_name, Category.name)
            .join(Category, Category.id == Transaction.category_id)
            .filter(Transaction.payee.in_(payees_needed))
            .distinct()
            .all()
        ) if payees_needed else []
        history_map = defaultdict(set)
        for payee, group, name in history_rows:
            history_map[payee].add(f"{group} > {name}")
        history_text = "\n".join(
            f"  {payee} → previously categorized as: {', '.join(sorted(cats))}"
            for payee, cats in history_map.items()
        )

        txn_list_text = "\n".join(
            f"  id={t.id} | {t.date} | {(t.payee or '(blank)')[:40]} | "
            f"{'+' if t.amount > 0 else '-'}${abs(t.amount)/100:,.2f}"
            + (f" | memo: {t.memo[:60]}" if t.memo else "")
            for t in candidates
        )

        prompt = (
            "Assign the best-fitting category to each uncategorized transaction below, "
            "using the payee name, memo, and amount (negative = expense, positive = income). "
            "Only choose from the category list provided — do not invent new categories.\n\n"
            f"## Available Categories\n{cat_list_text}\n\n"
            + (f"## Payee History (how similar payees were categorized before)\n{history_text}\n\n"
               if history_text else "")
            + f"## Uncategorized Transactions\n{txn_list_text}\n\n"
            "Call the submit_category_suggestions tool with your suggestions. "
            "Only include a suggestion when you're reasonably confident. "
            "Skip transactions that are too ambiguous to categorize confidently."
        )

        try:
            parsed = _ai_tool_call(
                prompt,
                tool_name="submit_category_suggestions",
                tool_description="Submit category suggestions for uncategorized transactions.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "suggestions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "transaction_id": {"type": "integer", "description": "The transaction's id, exactly as given."},
                                    "category": {"type": "string", "description": "Exact 'Group > Name' from the provided category list."},
                                    "reason": {"type": "string", "description": "Brief explanation of the choice."},
                                },
                                "required": ["transaction_id", "category", "reason"],
                            },
                        },
                    },
                    "required": ["suggestions"],
                },
                # Up to 150 candidates, and unlike payee cleanup every one of
                # them can legitimately get a suggestion — give it plenty of
                # room so a big backlog doesn't get truncated into looking
                # like "nothing to suggest" (see _ai_tool_call).
                max_tokens=16384,
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        suggestions = parsed.get("suggestions", [])
        candidate_ids = {t.id for t in candidates}

        # Replace existing pending suggestions
        AiCategorySuggestion.query.filter_by(status="pending").delete()

        created = []
        for s in suggestions:
            tid = s.get("transaction_id")
            category_text = (s.get("category") or "").strip()
            reason = (s.get("reason") or "").strip()
            if not isinstance(tid, int) or tid not in candidate_ids or ">" not in category_text:
                continue
            group, name = (part.strip() for part in category_text.split(">", 1))
            cat = cat_lookup.get((group.lower(), name.lower()))
            if not cat:
                continue
            db.session.add(AiCategorySuggestion(
                transaction_id=tid,
                suggested_category_id=cat.id,
                reason=reason,
                status="pending",
            ))
            created.append({
                "transaction_id": tid,
                "category_id": cat.id,
                "category": f"{cat.group_name} > {cat.name}",
                "reason": reason,
            })

        db.session.commit()
        return jsonify({"suggestions": created, "count": len(created)})

    @app.route("/api/ai/approve-category/<int:sid>", methods=["POST"])
    @login_required
    def api_ai_approve_category(sid):
        suggestion = AiCategorySuggestion.query.get_or_404(sid)
        if suggestion.status != "pending":
            return jsonify({"error": "already processed"}), 400
        t = db.session.get(Transaction, suggestion.transaction_id)
        if not t:
            suggestion.status = "rejected"
            db.session.commit()
            return jsonify({"error": "transaction no longer exists"}), 400
        if t.category_id is not None or t.splits:
            suggestion.status = "rejected"
            db.session.commit()
            return jsonify({"error": "transaction already categorized"}), 400

        category_id = request.form.get("category_id", type=int)
        if category_id:
            category = db.session.get(Category, category_id)
            if not category:
                return jsonify({"error": "unknown category"}), 400
            t.category_id = category.id
        else:
            t.category_id = suggestion.suggested_category_id
        suggestion.status = "approved"
        db.session.commit()
        return jsonify({"ok": True, "category_id": t.category_id})

    @app.route("/api/ai/reject-category/<int:sid>", methods=["POST"])
    @login_required
    def api_ai_reject_category(sid):
        suggestion = AiCategorySuggestion.query.get_or_404(sid)
        if suggestion.status != "pending":
            return jsonify({"error": "already processed"}), 400
        suggestion.status = "rejected"
        db.session.commit()
        return jsonify({"ok": True})

    # ---- health & error pages ----
    @app.route("/healthz")
    def healthz():
        """Unauthenticated health endpoint for monitoring / load balancers."""
        return {"status": "ok"}, 200

    @app.errorhandler(403)
    def _403(_):
        return render_template("error.html", code=403,
                               message="You don't have access to that."), 403

    @app.errorhandler(404)
    def _404(_):
        return render_template("error.html", code=404,
                               message="That page doesn't exist."), 404

    @app.errorhandler(429)
    def _429(_):
        return render_template("error.html", code=429,
                               message="Too many attempts. Try again in a few minutes."), 429


# ---------- query helpers ----------

def _assigned_for(year: int, month: int, category_id: int) -> int:
    bm = BudgetMonth.query.filter_by(year=year, month=month,
                                      category_id=category_id).first()
    return bm.assigned if bm else 0


def _spent_for(year: int, month: int, category_id: int) -> int:
    total = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
             .filter(Transaction.category_id == category_id,
                     extract("year", Transaction.date) == year,
                     extract("month", Transaction.date) == month)
             .scalar())
    return int(total or 0)


def _carryover_for(year: int, month: int, category_id: int) -> int:
    prior_assigned = (db.session.query(func.coalesce(func.sum(BudgetMonth.assigned), 0))
                      .filter(BudgetMonth.category_id == category_id,
                              ((BudgetMonth.year < year) |
                               ((BudgetMonth.year == year) & (BudgetMonth.month < month))))
                      .scalar() or 0)
    first_of_month = date(year, month, 1)
    prior_spent = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
                   .filter(Transaction.category_id == category_id,
                           Transaction.date < first_of_month)
                   .scalar() or 0)
    return int(prior_assigned + prior_spent)


def _month_totals(year: int, month: int) -> tuple[int, int]:
    assigned = (db.session.query(func.coalesce(func.sum(BudgetMonth.assigned), 0))
                .filter_by(year=year, month=month).scalar() or 0)
    spent = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
             .join(Category, Category.id == Transaction.category_id)
             .filter(extract("year", Transaction.date) == year,
                     extract("month", Transaction.date) == month,
                     Category.is_income.is_(False))
             .scalar() or 0)
    return int(assigned), int(spent)


def _income_total(year: int, month: int) -> int:
    total = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
             .join(Category, Category.id == Transaction.category_id)
             .filter(extract("year", Transaction.date) == year,
                     extract("month", Transaction.date) == month,
                     Category.is_income.is_(True))
             .scalar() or 0)
    return int(total)


def _prior_surplus(year: int, month: int) -> int:
    first = date(year, month, 1)
    prior_income = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
                    .join(Category, Category.id == Transaction.category_id)
                    .filter(Transaction.date < first,
                            Category.is_income.is_(True))
                    .scalar() or 0)
    prior_assigned = (db.session.query(func.coalesce(func.sum(BudgetMonth.assigned), 0))
                      .filter((BudgetMonth.year < year) |
                              ((BudgetMonth.year == year) & (BudgetMonth.month < month)))
                      .scalar() or 0)
    return int(prior_income - prior_assigned)


def _prev_month(y, m): return (y - 1, 12) if m == 1 else (y, m - 1)
def _next_month(y, m): return (y + 1, 1) if m == 12 else (y, m + 1)


def _months_ago(n: int) -> date:
    today = date.today()
    y, m = today.year, today.month
    for _ in range(n):
        y, m = _prev_month(y, m)
    return date(y, m, 1)


def _income_expense_series(months: int = 6, start_date: date = None, end_date: date = None):
    labels = []
    income = []
    expense = []
    today = date.today()
    if start_date is None or end_date is None:
        y, m = today.year, today.month
        sequence = []
        for _ in range(months):
            sequence.append((y, m))
            y, m = _prev_month(y, m)
        sequence = list(reversed(sequence))
    else:
        y, m = start_date.year, start_date.month
        ey, em = end_date.year, end_date.month
        sequence = []
        while (y, m) <= (ey, em):
            sequence.append((y, m))
            y, m = _next_month(y, m)
    for (yy, mm) in sequence:
        labels.append(f"{yy}-{mm:02d}")
        inc = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
               .filter(extract("year", Transaction.date) == yy,
                       extract("month", Transaction.date) == mm,
                       Transaction.amount > 0).scalar() or 0)
        exp = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
               .filter(extract("year", Transaction.date) == yy,
                       extract("month", Transaction.date) == mm,
                       Transaction.amount < 0).scalar() or 0)
        income.append(round(int(inc) / 100.0, 2))
        expense.append(round(abs(int(exp)) / 100.0, 2))
    return labels, income, expense


def _parse_report_range(req):
    """Return (start_date, end_date, preset) from request args."""
    preset = req.args.get("preset", "this_month")
    today = date.today()
    if preset == "this_month":
        start = date(today.year, today.month, 1)
        end = today
    elif preset == "last_month":
        y, m = _prev_month(today.year, today.month)
        start = date(y, m, 1)
        end = date(y, m, monthrange(y, m)[1])
    elif preset == "this_year":
        start = date(today.year, 1, 1)
        end = today
    elif preset == "last_year":
        start = date(today.year - 1, 1, 1)
        end = date(today.year - 1, 12, 31)
    elif preset.endswith("d") and preset.startswith("last_"):
        try:
            days = int(preset[5:-1])
        except ValueError:
            days = 30
            preset = "last_30d"
        start = today - timedelta(days=days - 1)
        end = today
    elif preset == "custom":
        try:
            start = datetime.strptime(req.args["start_date"], "%Y-%m-%d").date()
            end = datetime.strptime(req.args["end_date"], "%Y-%m-%d").date()
            if start > end:
                start, end = end, start
        except (KeyError, ValueError):
            start = _months_ago(6)
            end = today
            preset = "last_6"
    else:
        try:
            n = int(preset.replace("last_", ""))
        except ValueError:
            n = 6
            preset = "last_6"
        start = _months_ago(n)
        end = today
    return start, end, preset


def _net_worth_series(months: int = 12, start_date: date = None, end_date: date = None):
    today = date.today()
    if start_date is not None and end_date is not None:
        y, m = start_date.year, start_date.month
        ey, em = end_date.year, end_date.month
        points = []
        while (y, m) <= (ey, em):
            points.append((y, m))
            y, m = _next_month(y, m)
    else:
        y, m = today.year, today.month
        points = []
        for _ in range(months):
            points.append((y, m))
            y, m = _prev_month(y, m)
        points.reverse()

    accounts = Account.query.all()
    starting_sum = sum(a.starting_balance for a in accounts)

    labels, values = [], []
    for (yy, mm) in points:
        last_day = date(yy, mm, monthrange(yy, mm)[1])
        txn_sum = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
                   .filter(Transaction.date <= last_day).scalar() or 0)
        labels.append(f"{yy}-{mm:02d}")
        values.append(round((starting_sum + int(txn_sum)) / 100.0, 2))
    return labels, values


# ---------- gunicorn entry point ----------
# gunicorn runs `app:app` where `app` is the WSGI callable.
app = create_app()
