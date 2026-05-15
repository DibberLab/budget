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
import os
import secrets
import sys
import uuid
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

import click
from flask import (Flask, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import extract, func
from werkzeug.middleware.proxy_fix import ProxyFix

import csv_import
import plaid_client
from auth import auth_bp, login_manager, setup_required
from models import (MAX_USERS, Account, BudgetMonth, Category, Goal,
                    PlaidItem, Recurring, Transaction, User, db, fmt_money,
                    from_cents, to_cents)
from recurring import post_due_recurring


csrf = CSRFProtect()
limiter = Limiter(get_remote_address, default_limits=[])


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


def _seed_default_categories():
    """Create a starter set of categories if the DB is empty."""
    if Category.query.first():
        return
    defaults = [
        ("Income", "Paycheck", True),
        ("Income", "Other Income", True),
        ("Bills", "Rent / Mortgage", False),
        ("Bills", "Utilities", False),
        ("Bills", "Internet", False),
        ("Bills", "Phone", False),
        ("Bills", "Insurance", False),
        ("Everyday", "Groceries", False),
        ("Everyday", "Dining Out", False),
        ("Everyday", "Transportation", False),
        ("Everyday", "Personal", False),
        ("Quality of Life", "Subscriptions", False),
        ("Quality of Life", "Entertainment", False),
        ("Quality of Life", "Hobbies", False),
        ("Savings & Goals", "Emergency Fund", False),
        ("Savings & Goals", "Vacation", False),
    ]
    for group, name, is_income in defaults:
        db.session.add(Category(group_name=group, name=name, is_income=is_income))
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


# ---------- routes ----------

def register_routes(app: Flask):

    # ---- dashboard ----
    @app.route("/")
    @setup_required
    @login_required
    def dashboard():
        today = date.today()
        ym = (today.year, today.month)

        accounts = Account.query.filter_by(closed=False).all()
        on_budget_total = sum(a.balance for a in accounts if a.on_budget)
        off_budget_total = sum(a.balance for a in accounts if not a.on_budget)
        net_worth = on_budget_total + off_budget_total

        assigned, spent = _month_totals(*ym)
        income_this_month = _income_total(*ym)

        recent = (Transaction.query
                  .order_by(Transaction.date.desc(), Transaction.id.desc())
                  .limit(10).all())

        labels, income_series, expense_series = _income_expense_series(months=6)

        return render_template("dashboard.html",
                               accounts=accounts,
                               net_worth=net_worth,
                               on_budget_total=on_budget_total,
                               off_budget_total=off_budget_total,
                               assigned=assigned,
                               spent=spent,
                               income_this_month=income_this_month,
                               recent=recent,
                               chart_labels=labels,
                               chart_income=income_series,
                               chart_expense=expense_series,
                               year=today.year,
                               month=today.month)

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
        account_id = request.args.get("account_id", type=int)
        category_id = request.args.get("category_id", type=int)
        q = Transaction.query
        if account_id:
            q = q.filter_by(account_id=account_id)
        if category_id:
            q = q.filter_by(category_id=category_id)
        txns = q.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(500).all()
        accounts = Account.query.order_by(Account.name).all()
        categories = Category.query.order_by(Category.group_name, Category.name).all()
        return render_template("transactions.html",
                               txns=txns,
                               accounts=accounts,
                               categories=categories,
                               filter_account=account_id,
                               filter_category=category_id)

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
        cid = request.form.get("category_id") or None
        t.category_id = int(cid) if cid else None
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

    # ---- reports ----
    @app.route("/reports")
    @login_required
    def reports():
        months = request.args.get("months", default=6, type=int)
        labels, income_series, expense_series = _income_expense_series(months=months)

        cutoff = _months_ago(months)
        cat_totals = (db.session.query(Category.name, func.sum(Transaction.amount))
                      .join(Transaction, Transaction.category_id == Category.id)
                      .filter(Transaction.date >= cutoff,
                              Transaction.amount < 0,
                              Category.is_income.is_(False))
                      .group_by(Category.id).all())
        cat_labels = [r[0] for r in cat_totals]
        cat_values = [abs(int(r[1] or 0)) for r in cat_totals]

        nw_labels, nw_values = _net_worth_series(months=max(months, 12))

        return render_template("reports.html",
                               months=months,
                               chart_labels=labels,
                               chart_income=income_series,
                               chart_expense=expense_series,
                               cat_labels=cat_labels,
                               cat_values=cat_values,
                               nw_labels=nw_labels,
                               nw_values=nw_values)

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


def _income_expense_series(months: int = 6):
    labels = []
    income = []
    expense = []
    today = date.today()
    y, m = today.year, today.month
    sequence = []
    for _ in range(months):
        sequence.append((y, m))
        y, m = _prev_month(y, m)
    for (yy, mm) in reversed(sequence):
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


def _net_worth_series(months: int = 12):
    today = date.today()
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
