"""
Budget v2 — a deliberately small front-end over the same database as v1.

Three screens, mounted at /v2:
  /v2/          Progress: this month's spending vs. budget, one bar per category.
  /v2/review    Uncategorized transactions, grouped by payee. Clean up the payee
                name, pick a category, save — the choice is remembered as a
                PayeeRule so future imports of that payee are handled automatically.
  /v2/import    Upload a bank CSV. Rules are applied on the way in.
  /manage       Payees and categories by hand: refile a payee's history, edit
                its rule, rename/move/retire/add categories.

v2 shares login, users, accounts, categories, budgets, and transactions with v1,
so both versions can run side by side until v1 is retired.
"""
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import login_required
from sqlalchemy import extract, func

import csv_import
import plaid_client
from models import Account, BudgetMonth, Category, PlaidItem, Transaction, db

v2_bp = Blueprint("v2", __name__, template_folder="templates")


@v2_bp.route("/v2/", defaults={"path": ""})
@v2_bp.route("/v2/<path:path>")
def legacy_v2(path):
    """v2 used to live under /v2 while v1 was the main app; keep old links working."""
    qs = request.query_string.decode()
    return redirect("/" + path + (f"?{qs}" if qs else ""), code=301)


class PayeeRule(db.Model):
    """Remembers how a raw bank payee string should be cleaned up and categorized.

    Matching is exact on the lowercased raw payee — predictable, and the review
    screen's AI fill handles the long tail of variants.
    """
    __tablename__ = "payee_rules"
    id = db.Column(db.Integer, primary_key=True)
    match = db.Column(db.String(200), unique=True, nullable=False, index=True)
    payee = db.Column(db.String(200), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))

    category = db.relationship("Category")


class IncomePlan(db.Model):
    """Planned monthly income per income category. Kept separate from
    BudgetMonth because v1 treats every BudgetMonth row as money assigned
    out of income, so planned income there would skew v1's "To Be Budgeted"."""
    __tablename__ = "income_plans"
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), primary_key=True)
    amount = db.Column(db.Integer, nullable=False, default=0)  # cents per month


class AutoCat(db.Model):
    """A transaction the AI categorized on its own during import. Stays here
    until someone confirms (or corrects) it on the Review page, at which point
    the choice becomes a PayeeRule and this row is removed."""
    __tablename__ = "auto_categorized"
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id", ondelete="CASCADE"),
                               primary_key=True)
    raw_payee = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transaction = db.relationship("Transaction")


def _window(months: int):
    """(start, end, n) for the last `months` complete calendar months, clipped
    to where transaction history begins (skipping a partial first month)."""
    span = _full_months()
    if not span:
        return None
    start, end = span
    want = date(end.year, end.month, 1)
    for _ in range(months - 1):
        want = date(want.year - (want.month == 1), (want.month - 2) % 12 + 1, 1)
    start = max(start, want)
    n = (end.year - start.year) * 12 + end.month - start.month + 1
    return start, end, n


def _avg_by_category(start, end, n, cat_ids) -> dict:
    """Average monthly net amount per category (signed cents: spending negative)."""
    return {cid: round(int(total or 0) / n) for cid, total in
            (db.session.query(Transaction.category_id, func.sum(Transaction.amount))
             .filter(Transaction.category_id.in_(cat_ids),
                     Transaction.date >= start, Transaction.date <= end)
             .group_by(Transaction.category_id))}


# ---------- helpers ----------

def _key(payee: str) -> str:
    return (payee or "").strip().lower()


def _rules_by_match() -> dict:
    return {r.match: r for r in PayeeRule.query.all()}


def _uncategorized_query():
    """Transactions that still need a category: not a transfer, not a split
    parent (its line-items carry the categories), not a split line-item."""
    return (Transaction.query
            .filter(Transaction.category_id.is_(None),
                    Transaction.parent_id.is_(None),
                    Transaction.transfer_id.is_(None))
            .filter(~Transaction.splits.any()))


def _find_rule(rules: dict, payee: str):
    """Exact match first; then prefix rules, written with a single trailing
    '*' (e.g. 'camp fire minnes*'), longest prefix winning. Bank strings with
    masked card numbers ('*******#*******') contain other asterisks and stay
    exact-match only."""
    k = _key(payee)
    if k in rules:
        return rules[k]
    prefixes = [m for m in rules if m.endswith("*") and "*" not in m[:-1] and k.startswith(m[:-1])]
    return rules[max(prefixes, key=len)] if prefixes else None


def _apply_rule(t: Transaction, rule: "PayeeRule") -> None:
    t.payee = rule.payee
    # An income rule (e.g. a paycheck) only ever files deposits, so a charge
    # from the same payee isn't counted as income.
    if rule.category_id and t.category_id is None:
        cat = db.session.get(Category, rule.category_id)
        # A rule pointing at a retired category leaves the transaction for
        # Review instead of filing it somewhere that no longer has a budget.
        if cat and not cat.hidden and not (cat.is_income and (t.amount or 0) <= 0):
            t.category_id = rule.category_id


def _history_category(payees: set) -> dict:
    """payee -> category_id, only where every past categorized transaction
    for that payee used the same category (unambiguous)."""
    if not payees:
        return {}
    rows = (db.session.query(Transaction.payee, Transaction.category_id)
            .filter(Transaction.payee.in_(payees),
                    Transaction.category_id.isnot(None))
            .distinct().all())
    seen = defaultdict(set)
    for p, cid in rows:
        seen[p].add(cid)
    return {p: next(iter(c)) for p, c in seen.items() if len(c) == 1}


def _category_options():
    cats = (Category.query.filter_by(hidden=False)
            .order_by(Category.is_income.desc(), Category.group_name, Category.name).all())
    groups = defaultdict(list)
    for c in cats:
        groups[c.group_name].append(c)
    return list(groups.items())


def _budgets_as_of(y: int, m: int, cat_ids) -> tuple[dict, dict]:
    """Budget in effect for each category in month (y, m), and the month it was
    set in. If a category has no row this month, its most recent earlier row
    carries forward, so a budget only has to be set once. An explicit $0 row
    ends the carry-forward."""
    rows = (BudgetMonth.query
            .filter(BudgetMonth.category_id.in_(cat_ids),
                    (BudgetMonth.year * 12 + BudgetMonth.month) <= y * 12 + m)
            .order_by(BudgetMonth.year, BudgetMonth.month).all())
    budget, budget_from = {}, {}
    for b in rows:  # ascending, so the latest wins
        budget[b.category_id] = b.assigned
        budget_from[b.category_id] = (b.year, b.month)
    return budget, budget_from


def _full_months():
    """(first, last) of the complete calendar months covered by transaction
    history: skips a partial first month and the current month."""
    first = db.session.query(func.min(Transaction.date)).scalar()
    today = date.today()
    if not first:
        return None
    start = first if first.day == 1 else (date(first.year + (first.month == 12), first.month % 12 + 1, 1))
    end = date(today.year, today.month, 1) - timedelta(days=1)
    return (start, end) if start <= end else None


@v2_bp.app_context_processor
def _inject_counts():
    # Only the v2 templates use this; cheap enough to compute per request.
    if request.blueprint != "v2":
        return {}
    return {"v2_uncategorized": _uncategorized_query().count(),
            "v2_to_review": _uncategorized_query().count() + AutoCat.query.count()}


# ---------- progress ----------

def _month_add(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


def _chart_history(y: int, m: int, cats) -> dict:
    """Data for the Home charts: six months of spending/budget/income, the
    household's usual within-month spending path (from the three full months
    before this one, so bills on fixed dates are part of "normal"), last
    month's day-by-day spending, and which categories are everyday spending
    (three or more charges a month on average) rather than bills."""
    expense_ids = [c.id for c in cats if not c.is_income]
    income_ids = [c.id for c in cats if c.is_income]
    start_y, start_m = _month_add(y, m, -5)
    end_y, end_m = _month_add(y, m, 1)
    rows = (db.session.query(Transaction.date, Transaction.category_id, Transaction.amount)
            .filter(Transaction.category_id.isnot(None),
                    Transaction.date >= date(start_y, start_m, 1), Transaction.date < date(end_y, end_m, 1))
            .all())
    by_month = defaultdict(lambda: {"daily": defaultdict(int), "spent": 0, "income": 0, "amounts": defaultdict(list)})
    exp_set, inc_set = set(expense_ids), set(income_ids)
    for d, cid, amt in rows:
        mm = by_month[(d.year, d.month)]
        if cid in exp_set:
            mm["daily"][d.day] -= amt
            mm["spent"] -= amt
            mm["amounts"][cid].append(-amt)
        elif cid in inc_set:
            mm["income"] += amt

    history = []
    for k in range(-5, 1):
        yy, mo = _month_add(y, m, k)
        b, _ = _budgets_as_of(yy, mo, expense_ids)
        mm = by_month.get((yy, mo))
        history.append({"label": date(yy, mo, 1).strftime("%b"), "full": date(yy, mo, 1).strftime("%B %Y"),
                        "spent": mm["spent"] if mm else 0, "income": mm["income"] if mm else 0,
                        "budget": sum(b.values())})

    days = monthrange(y, m)[1]
    curves = []
    for k in (-3, -2, -1):
        yy, mo = _month_add(y, m, k)
        mm = by_month.get((yy, mo))
        if not mm or mm["spent"] <= 0:
            continue
        dm = monthrange(yy, mo)[1]
        cum, run = [0.0] * (dm + 1), 0
        for day in range(1, dm + 1):
            run += mm["daily"].get(day, 0)
            cum[day] = run / mm["spent"]
        curves.append([cum[min(dm, round(d * dm / days))] for d in range(days + 1)])
    usual = [sum(c[d] for c in curves) / len(curves) for d in range(days + 1)] if curves else None

    py, pm = _month_add(y, m, -1)
    last = by_month.get((py, pm))
    last_days = monthrange(py, pm)[1]
    last_cum, run = [], 0
    for day in range(1, last_days + 1):
        run += last["daily"].get(day, 0) if last else 0
        last_cum.append(run)

    # Everyday = frequent AND small: 3+ charges a month with a typical (median)
    # charge under $150. Loan or card payments that happen often aren't everyday.
    amounts = defaultdict(list)
    for k in (-3, -2, -1):
        mm = by_month.get(_month_add(y, m, k))
        if mm:
            for cid, xs in mm["amounts"].items():
                amounts[cid].extend(xs)
    everyday = sorted(cid for cid, xs in amounts.items()
                      if len(xs) / 3 >= 3 and sorted(xs)[len(xs) // 2] < 15000)
    return {"history": history, "usual": usual, "usual_months": len(curves),
            "last": last_cum, "last_label": date(py, pm, 1).strftime("%B"), "everyday": everyday}


@v2_bp.route("/")
@login_required
def progress():
    """Home: the month as a spreadsheet-style budget (income, every category's
    monthly amount and what's been spent, Extra $$), with the charts above it
    folded away until wanted. This month is editable; past months are read-only."""
    today = date.today()
    try:
        y = int(request.args.get("y", today.year))
        m = int(request.args.get("m", today.month))
        date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
    limit = _month_add(today.year, today.month, 12)
    if (y, m) > limit:
        y, m = limit
    is_current = (y, m) == (today.year, today.month)
    is_future = (y, m) > (today.year, today.month)

    cats = Category.query.order_by(Category.group_name, Category.name).all()
    ids = [c.id for c in cats]
    plan, _ = _budgets_as_of(y, m, ids)
    ny, nm = (y + (m == 12), m % 12 + 1)
    nxt_plan, _ = _budgets_as_of(ny, nm, ids)
    income_plan = {p.category_id: p.amount for p in IncomePlan.query}
    spent = {cid: int(total or 0) for cid, total in
             db.session.query(Transaction.category_id, func.sum(Transaction.amount))
             .filter(Transaction.category_id.in_(ids),
                     extract("year", Transaction.date) == y, extract("month", Transaction.date) == m)
             .group_by(Transaction.category_id)}

    # "avg": a typical month for each category, from the six full months
    # before this one (months before the household's history began don't
    # count). Unusual months - a month the mortgage skipped or paid twice
    # because it goes out on the 28th-1st, a three-paycheck month, a one-off
    # splurge - are left out: only months within ±40% of the category's
    # median month are averaged. A category that's usually empty (median $0)
    # just averages every month, so occasional spending still shows up.
    first = date(y, m, 1)
    a_start = date(*_month_add(y, m, -6), 1)
    earliest = db.session.query(func.min(Transaction.date)).scalar()
    if earliest:
        hist_start = earliest if earliest.day == 1 else date(*_month_add(earliest.year, earliest.month, 1), 1)
        a_start = max(a_start, hist_start)
    months = []
    cy, cm = a_start.year, a_start.month
    while (cy, cm) < (y, m):
        months.append((cy, cm)); cy, cm = _month_add(cy, cm, 1)
    monthly = defaultdict(lambda: defaultdict(int))
    if months:
        for cid, yy, mo, total in (db.session.query(Transaction.category_id, extract("year", Transaction.date),
                                                     extract("month", Transaction.date), func.sum(Transaction.amount))
                                   .filter(Transaction.category_id.in_(ids), Transaction.date >= a_start, Transaction.date < first)
                                   .group_by(Transaction.category_id, extract("year", Transaction.date), extract("month", Transaction.date))):
            monthly[cid][(int(yy), int(mo))] = int(total or 0)
    mname = lambda ym: date(ym[0], ym[1], 1).strftime("%b")

    def typical(cid):
        """(average in cents, signed like the transactions; hover note)."""
        if not months:
            return 0, "No earlier months to average yet"
        vals = [(ym, abs(monthly[cid].get(ym, 0))) for ym in months]
        amounts = sorted(v for _, v in vals)
        n = len(amounts)
        med = amounts[n // 2] if n % 2 else (amounts[n // 2 - 1] + amounts[n // 2]) / 2
        keep = [(ym, v) for ym, v in vals if med and 0.6 * med <= v <= 1.4 * med]
        sign = -1 if sum(monthly[cid].values()) < 0 else 1
        if not keep:
            avg = sum(v for _, v in vals) / n
            return sign * avg, f"Average of {mname(months[0])}–{mname(months[-1])} (spending here is occasional)"
        avg = sum(v for _, v in keep) / len(keep)
        skipped = [ym for ym, _ in vals if (ym, dict(vals)[ym]) not in keep]
        note = "Typical month, from " + ", ".join(mname(ym) for ym, _ in keep)
        if skipped:
            note += " · left out as unusual: " + ", ".join(mname(ym) for ym in skipped)
        return sign * avg, note

    avg3, avg_note = {}, {}
    for cid in ids:
        avg3[cid], avg_note[cid] = typical(cid)

    income, groups = [], defaultdict(list)
    for c in cats:
        if c.is_income:
            if not c.hidden or spent.get(c.id):
                p = income_plan.get(c.id, 0)
                income.append({"cat": c, "plan": p, "next": p, "spent": spent.get(c.id, 0),
                               "avg": round(avg3.get(c.id, 0)), "avg_note": avg_note.get(c.id, "")})
            continue
        s_ = -spent.get(c.id, 0)  # outflow as positive
        if c.hidden:
            # Retired categories only show in months where they still have spending.
            if s_:
                groups["Retired"].append({"cat": c, "plan": 0, "next": 0, "spent": s_,
                                          "avg": round(-avg3.get(c.id, 0)), "avg_note": avg_note.get(c.id, "")})
            continue
        groups[c.group_name].append({"cat": c, "plan": plan.get(c.id, 0),
                                     "next": nxt_plan.get(c.id, 0) if is_current else plan.get(c.id, 0),
                                     "spent": s_, "avg": round(-avg3.get(c.id, 0)), "avg_note": avg_note.get(c.id, "")})
    for items in groups.values():
        items.sort(key=lambda r: (-r["plan"], -r["spent"], r["cat"].name.lower()))
    # Biggest groups first; Retired (spending with no budget) last.
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "Retired", -sum(r["plan"] for r in kv[1]), kv[0].lower()))

    days_in_month = monthrange(y, m)[1]
    days_elapsed = today.day if is_current else (0 if is_future else days_in_month)
    pace = round(100 * days_elapsed / days_in_month)
    daily = [0] * days_in_month
    for d, total in (db.session.query(Transaction.date, func.sum(Transaction.amount))
                     .join(Category, Category.id == Transaction.category_id)
                     .filter(Category.is_income.is_(False),
                             extract("year", Transaction.date) == y, extract("month", Transaction.date) == m)
                     .group_by(Transaction.date)):
        daily[d.day - 1] -= int(total or 0)

    prev = (y - 1, 12) if m == 1 else (y, m - 1)
    return render_template(
        "v2/month.html", y=y, m=m, income=income, groups=ordered,
        month_name=date(y, m, 1).strftime("%B"), month_label=date(y, m, 1).strftime("%B %Y"),
        next_label=date(ny, nm, 1).strftime("%B"),
        prev_label=date(prev[0], prev[1], 1).strftime("%B"),
        prev=prev, nxt=(ny, nm), is_current=is_current, is_future=is_future,
        editable=is_current or is_future, can_next=(ny, nm) <= limit,
        avg_label=(f"{mname(months[0])}–{mname(months[-1])}" if months else "no earlier months"),
        cat_options=[(g, [{"id": c.id, "name": c.name} for c in cs]) for g, cs in _category_options()],
        pace=pace, daily=daily, days_in_month=days_in_month, days_elapsed=days_elapsed,
        charts=_chart_history(y, m, cats))


@v2_bp.route("/category/<int:cid>")
@login_required
def category_detail(cid):
    """Small JSON list of a category's transactions for a month (progress drill-down)."""
    y, m = request.args.get("y", type=int), request.args.get("m", type=int)
    if not y or not m:
        abort(400)
    txns = (Transaction.query
            .filter(Transaction.category_id == cid,
                    extract("year", Transaction.date) == y,
                    extract("month", Transaction.date) == m)
            .order_by(Transaction.date.desc()).all())
    return jsonify([{"date": t.date.strftime("%b %-d"), "iso": t.date.isoformat(), "id": t.id,
                     "payee": t.payee, "amount": -t.amount} for t in txns])


# ---------- plan vs actual ----------

@v2_bp.route("/plan")
@login_required
def plan():
    today = date.today()
    months = request.args.get("months", 3, type=int)
    months = months if months in (1, 3, 6, 12) else 3
    win = _window(months)

    cats = (Category.query.order_by(Category.is_income.desc(), Category.sort_order,
                                    Category.group_name, Category.name).all())
    cat_ids = [c.id for c in cats]
    budget, _ = _budgets_as_of(today.year, today.month, cat_ids)
    incplan = {p.category_id: p.amount for p in IncomePlan.query}
    actual = _avg_by_category(*win, cat_ids) if win else {}

    groups = defaultdict(lambda: {"rows": [], "plan": 0, "actual": 0})
    retired = 0
    inc = {"plan": 0, "actual": 0}
    for c in cats:
        a = actual.get(c.id, 0)
        if c.is_income:
            p = incplan.get(c.id, 0)
            inc["plan"] += p
            inc["actual"] += a
            if p or a:
                groups["Income"]["rows"].append({"cat": c, "plan": p, "actual": a, "diff": a - p})
            continue
        a = -a  # spending as positive
        if c.hidden:
            retired += a
            continue
        p = budget.get(c.id, 0)
        g = groups[c.group_name]
        g["rows"].append({"cat": c, "plan": p, "actual": a, "diff": a - p})
        g["plan"] += p
        g["actual"] += a
    for k in ("Income",):
        if k in groups:
            groups[k]["plan"], groups[k]["actual"] = inc["plan"], inc["actual"]

    spend = {"plan": sum(g["plan"] for k, g in groups.items() if k != "Income"),
             "actual": sum(g["actual"] for k, g in groups.items() if k != "Income") + retired}
    biggest = max([abs(r["diff"]) for g in groups.values() for r in g["rows"]] + [1])
    uncategorized = (_uncategorized_query()
                     .filter(Transaction.date >= win[0], Transaction.date <= win[1]).count()) if win else 0

    # Month-by-month totals for the trend chart: the window's months, but at
    # least the last 6 full months (where history allows) for context.
    trend = []
    span = _full_months()
    if span:
        first, end = span
        n_chart = max(months, 6)
        cur = date(end.year, end.month, 1)
        months_list = []
        while len(months_list) < n_chart and cur >= date(first.year, first.month, 1):
            months_list.append(cur)
            cur = date(cur.year - (cur.month == 1), (cur.month - 2) % 12 + 1, 1)
        totals = defaultdict(lambda: {"spend": 0, "income": 0})
        if months_list:
            for yy, mm, is_inc, total in (
                    db.session.query(extract("year", Transaction.date), extract("month", Transaction.date),
                                     Category.is_income, func.sum(Transaction.amount))
                    .join(Category, Category.id == Transaction.category_id)
                    .filter(Transaction.date >= months_list[-1], Transaction.date <= end)
                    .group_by(extract("year", Transaction.date), extract("month", Transaction.date),
                              Category.is_income)):
                key = (int(yy), int(mm))
                if is_inc:
                    totals[key]["income"] += int(total or 0)
                else:
                    totals[key]["spend"] -= int(total or 0)
        in_win = {(d.year, d.month) for d in months_list[:win[2]]} if win else set()
        for d in reversed(months_list):
            t = totals[(d.year, d.month)]
            trend.append({"label": d.strftime("%b"), "spend": t["spend"], "income": t["income"],
                          "in_window": (d.year, d.month) in in_win})

    for g in groups.values():
        g["rows"].sort(key=lambda r: (-r["plan"], -r["actual"], r["cat"].name.lower()))

    return render_template(
        "v2/plan.html", months=months, win=win, groups=list(groups.items()),
        inc=inc, spend=spend, retired=retired, biggest=biggest, uncategorized=uncategorized,
        trend=trend)


# ---------- budgets ----------

@v2_bp.route("/budgets")
@v2_bp.route("/budgets/sheet")
@login_required
def budgets():
    """The Planner and Sheet pages were folded into the home page."""
    return redirect(url_for("v2.progress"))


@v2_bp.route("/budgets/save", methods=["POST"])
@login_required
def budgets_save():
    """Body: {"budgets": {category_id: cents}, "y": int?, "m": int?}. Sets each
    budget from that month (default: this month) onward; earlier months keep
    what they had. Past months can't be changed."""
    body = request.get_json(silent=True) or {}
    data = body.get("budgets", {})
    income = {c.id for c in Category.query.filter_by(is_income=True)}
    cats = {c.id: c for c in Category.query}
    valid = set(cats)
    today = date.today()
    try:
        y, m = int(body.get("y") or today.year), int(body.get("m") or today.month)
        date(y, m, 1)
    except (TypeError, ValueError):
        return jsonify({"error": "Bad month."}), 400
    if (y, m) < (today.year, today.month):
        return jsonify({"error": "Past months can't be changed."}), 400
    changed = 0
    for cid, cents in data.items():
        try:
            cid, cents = int(cid), int(cents)
        except (TypeError, ValueError):
            continue
        if cid not in valid or cents < 0:
            continue
        if cid in income:
            plan = db.session.get(IncomePlan, cid) or IncomePlan(category_id=cid)
            plan.amount = cents
            db.session.add(plan)
            changed += 1
            continue
        set_budget(cats[cid], cents, y=y, m=m)
        changed += 1
    db.session.commit()
    return jsonify({"ok": True, "changed": changed})


def _copy_month_args():
    """(y, m, prev_y, prev_m) from the request, or an error response. Only
    the current month or later can be changed."""
    body = request.get_json(silent=True) or {}
    src = request.args if request.method == "GET" else body
    today = date.today()
    try:
        y, m = int(src.get("y")), int(src.get("m"))
        date(y, m, 1)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "Bad month."}), 400)
    if (y, m) < (today.year, today.month):
        return None, (jsonify({"error": "Past months can't be changed."}), 400)
    py, pm = _month_add(y, m, -1)
    return (y, m, py, pm), None


def _copy_month_diffs(y, m, py, pm):
    """Active expense categories whose budget for (y, m) differs from (py, pm)."""
    cats = (Category.query.filter_by(hidden=False, is_income=False)
            .order_by(Category.group_name, Category.name).all())
    ids = [c.id for c in cats]
    prev, _ = _budgets_as_of(py, pm, ids)
    cur, _ = _budgets_as_of(y, m, ids)
    return [{"id": c.id, "name": c.name, "group": c.group_name,
             "prev": prev.get(c.id, 0), "cur": cur.get(c.id, 0)}
            for c in cats if prev.get(c.id, 0) != cur.get(c.id, 0)], cats


@v2_bp.route("/budgets/copy-preview")
@login_required
def budgets_copy_preview():
    """What "copy from the previous month" would change for ?y=&m=."""
    args, err = _copy_month_args()
    if err:
        return err
    y, m, py, pm = args
    diffs, _ = _copy_month_diffs(y, m, py, pm)
    return jsonify({"month": date(y, m, 1).strftime("%B"), "prev": date(py, pm, 1).strftime("%B"), "changes": diffs})


@v2_bp.route("/budgets/copy", methods=["POST"])
@login_required
def budgets_copy():
    """Body: {"y", "m", "ids": [category ids]}. Sets those categories' budgets
    for (y, m) to what the previous month had. Only that month is written:
    later months that have their own amounts keep them."""
    args, err = _copy_month_args()
    if err:
        return err
    y, m, py, pm = args
    want = {int(i) for i in (request.get_json(silent=True) or {}).get("ids") or [] if str(i).isdigit()}
    diffs, _ = _copy_month_diffs(y, m, py, pm)
    changed = 0
    for d in diffs:
        if d["id"] not in want:
            continue
        row = (BudgetMonth.query.filter_by(year=y, month=m, category_id=d["id"]).first()
               or BudgetMonth(year=y, month=m, category_id=d["id"]))
        row.assigned = d["prev"]
        db.session.add(row)
        changed += 1
    db.session.commit()
    return jsonify({"ok": True, "changed": changed})


# ---------- review ----------

@v2_bp.route("/review")
@login_required
def review():
    txns = _uncategorized_query().order_by(Transaction.date.desc()).all()

    groups = {}
    for t in txns:
        g = groups.setdefault(_key(t.payee), {
            "raw": t.payee, "txns": [], "total": 0, "latest": t.date})
        g["txns"].append(t)
        g["total"] += t.amount

    rules = _rules_by_match()
    history = _history_category({g["raw"] for g in groups.values()})
    for k, g in groups.items():
        rule = _find_rule(rules, g["raw"])
        g["suggest_payee"] = rule.payee if rule else g["raw"]
        g["suggest_cat"] = (rule.category_id if rule and rule.category_id
                            else history.get(g["raw"]))
        g["key"] = k

    ordered = sorted(groups.values(), key=lambda g: (-len(g["txns"]), g["raw"].lower()))

    picks = {}
    for ac in (AutoCat.query.join(Transaction, Transaction.id == AutoCat.transaction_id)
               .order_by(Transaction.date.desc())):
        t = ac.transaction
        p = picks.setdefault(_key(t.payee), {"key": _key(t.payee), "payee": t.payee,
                                             "category_id": t.category_id, "txns": [],
                                             "total": 0, "raws": set()})
        p["txns"].append(t)
        p["total"] += t.amount
        p["raws"].add(ac.raw_payee)
    picks = sorted(picks.values(), key=lambda p: (-len(p["txns"]), p["payee"].lower()))

    return render_template("v2/review.html", groups=ordered, picks=picks,
                           category_groups=_category_options())


@v2_bp.route("/review/save", methods=["POST"])
@login_required
def review_save():
    """Body: {"items": [{"raw": str, "payee": str, "category_id": int|null,
                         "overrides": {txn_id: category_id}, "remember": bool}]}"""
    data = request.get_json(silent=True) or {}
    valid_cats = {c.id for c in Category.query.all()}
    renamed = categorized = 0

    for item in data.get("items", []):
        raw = (item.get("raw") or "").strip()
        new_payee = (item.get("payee") or "").strip() or raw
        cid = item.get("category_id")
        cid = cid if cid in valid_cats else None
        overrides = {int(k): v for k, v in (item.get("overrides") or {}).items()
                     if v in valid_cats}
        if not raw:
            continue

        # Rename the payee everywhere it appears (not just the uncategorized
        # rows) so history and reports group under one clean name.
        if new_payee != raw:
            renamed += (Transaction.query
                        .filter(func.lower(func.trim(Transaction.payee)) == _key(raw))
                        .update({"payee": new_payee}, synchronize_session=False))

        for t in (_uncategorized_query()
                  .filter(func.lower(func.trim(Transaction.payee)).in_({_key(raw), _key(new_payee)}))
                  .all()):
            target = overrides.get(t.id, cid)
            if target:
                t.category_id = target
                categorized += 1

        if item.get("remember", True) and (cid or new_payee != raw):
            for match in {_key(raw), _key(new_payee)}:
                rule = PayeeRule.query.filter_by(match=match).first() or PayeeRule(match=match)
                rule.payee = new_payee
                if cid:
                    rule.category_id = cid
                db.session.add(rule)

    db.session.commit()
    return jsonify({"ok": True, "renamed": renamed, "categorized": categorized})


# ---------- AI categorization ----------

AI_BATCH = 25  # payee groups per Claude call — keeps each request well under gunicorn's timeout


def _uncategorized_groups(skip=()) -> dict:
    """Uncategorized transactions grouped by raw payee, most frequent first."""
    groups = {}
    for t in _uncategorized_query().order_by(Transaction.date.desc()):
        k = _key(t.payee)
        if k in skip:
            continue
        g = groups.setdefault(k, {"raw": t.payee, "txns": [], "total": 0, "memo": ""})
        g["txns"].append(t)
        g["total"] += t.amount
        g["memo"] = g["memo"] or (t.memo or "")
    return dict(sorted(groups.items(), key=lambda kv: -len(kv[1]["txns"])))


def _ai_suggest(groups: dict) -> list[dict]:
    """Ask Claude for a clean payee name, a category, and whether it's sure,
    for each payee group. Returns [{key, payee, category_id, sure}]."""
    from app import _ai_tool_call  # late import: app.py imports this module

    cats = Category.query.filter_by(hidden=False).order_by(Category.group_name, Category.name).all()
    lookup = {(c.group_name.strip().lower(), c.name.strip().lower()): c.id for c in cats}
    cat_text = "\n".join(f"  {c.group_name} > {c.name}" + (" (income)" if c.is_income else "")
                         for c in cats)

    known = (db.session.query(Transaction.payee, Category.group_name, Category.name)
             .join(Category, Category.id == Transaction.category_id)
             .filter(Category.hidden.is_(False))
             .group_by(Transaction.payee, Category.group_name, Category.name)
             .order_by(func.count().desc()).limit(200).all())
    known_text = "\n".join(f"  {p} → {g} > {n}" for p, g, n in known)

    payee_text = "\n".join(
        f"  {g['raw']} | {len(g['txns'])} txn | {'+' if g['total'] > 0 else '-'}${abs(g['total'])/100:,.2f}"
        + (f" | memo: {g['memo'][:60]}" if g["memo"] else "")
        for g in groups.values())

    prompt = (
        "These are raw payee strings from a household's bank transactions that still need "
        "a category. For each one, give a short clean human-readable payee name "
        "(e.g. 'AMZN MKTP US*AB12CD3' → 'Amazon', 'SQ *JOES COFFEE 0423' → \"Joe's Coffee\") "
        "and the best category from the list. Reuse the exact clean names from the "
        "already-categorized examples when it's the same merchant. Negative = spending, "
        "positive = income.\n\n"
        "Set sure=true only when the merchant is clearly identifiable AND the category is "
        "obvious (e.g. a grocery chain → Groceries, or the same merchant appears in the "
        "examples). Set sure=false for ambiguous cases — person-to-person payments (Venmo, "
        "Zelle, checks), generic retailers that sell many kinds of things, or unfamiliar "
        "names. Leave category empty if you can't make a reasonable guess.\n\n"
        f"## Categories\n{cat_text}\n\n"
        + (f"## Already-categorized examples\n{known_text}\n\n" if known_text else "")
        + f"## Payees to clean up\n{payee_text}"
    )

    parsed = _ai_tool_call(
        prompt,
        tool_name="submit",
        tool_description="Submit a clean payee name and category for each raw payee.",
        input_schema={
            "type": "object",
            "properties": {"suggestions": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "raw": {"type": "string", "description": "Raw payee exactly as given."},
                    "payee": {"type": "string", "description": "Clean payee name."},
                    "category": {"type": "string",
                                 "description": "Exact 'Group > Name' from the list, or empty."},
                    "sure": {"type": "boolean",
                             "description": "True only if both the merchant and category are clear."},
                },
                "required": ["raw", "payee", "category", "sure"],
            }}},
            "required": ["suggestions"],
        },
        max_tokens=8192,
    )

    out = []
    for sug in parsed.get("suggestions", []):
        k = _key(sug.get("raw"))
        if k not in groups:
            continue
        cid = None
        if ">" in (sug.get("category") or ""):
            g, n = (p.strip().lower() for p in sug["category"].split(">", 1))
            cid = lookup.get((g, n))
        out.append({"key": k, "payee": (sug.get("payee") or "").strip(),
                    "category_id": cid, "sure": bool(sug.get("sure")) and cid is not None})
    return out


@v2_bp.route("/review/ai", methods=["POST"])
@login_required
def review_ai():
    """Run one batch of AI suggestions over uncategorized payee groups.

    Body: {"skip": [keys already processed this run], "apply": bool}
    With apply, suggestions the AI is sure about are saved immediately (and
    recorded as AutoCat for later confirmation); the rest come back as
    suggestions for the Review page to pre-fill. The client calls this
    repeatedly until `remaining` is 0."""
    body = request.get_json(silent=True) or {}
    skip = set(body.get("skip") or [])
    apply = bool(body.get("apply"))

    pending = _uncategorized_groups(skip)
    batch = dict(list(pending.items())[:AI_BATCH])
    if not batch:
        return jsonify({"suggestions": [], "keys": [], "remaining": 0, "applied": 0})

    try:
        suggestions = _ai_suggest(batch)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    applied, out = 0, []
    for sug in suggestions:
        g = batch[sug["key"]]
        if apply and sug["sure"]:
            for t in g["txns"]:
                db.session.add(AutoCat(transaction_id=t.id, raw_payee=t.payee))
                if sug["payee"]:
                    t.payee = sug["payee"]
                t.category_id = sug["category_id"]
                applied += 1
        else:
            out.append(sug)
    db.session.commit()
    return jsonify({"suggestions": out, "keys": list(batch), "applied": applied,
                    "remaining": max(0, len(pending) - len(batch))})


@v2_bp.route("/review/confirm", methods=["POST"])
@login_required
def review_confirm():
    """Confirm or correct AI picks. Body: {"items": [{"key": current payee key,
    "payee": str, "category_id": int}]}. Updates those transactions, remembers
    the choice as PayeeRules, and clears them from the AI-picks list."""
    valid_cats = {c.id for c in Category.query.all()}
    confirmed = 0
    for item in (request.get_json(silent=True) or {}).get("items", []):
        key = _key(item.get("key"))
        payee = (item.get("payee") or "").strip()
        cid = item.get("category_id")
        if cid not in valid_cats or not key:
            continue
        rows = (AutoCat.query.join(Transaction, Transaction.id == AutoCat.transaction_id)
                .filter(func.lower(func.trim(Transaction.payee)) == key).all())
        raws = set()
        for ac in rows:
            t = ac.transaction
            if payee:
                t.payee = payee
            t.category_id = cid
            raws.add(_key(ac.raw_payee))
            db.session.delete(ac)
            confirmed += 1
        for match in raws | {_key(payee or key)}:
            rule = PayeeRule.query.filter_by(match=match).first() or PayeeRule(match=match)
            rule.payee = payee or rule.payee or key
            rule.category_id = cid
            db.session.add(rule)
    db.session.commit()
    return jsonify({"ok": True, "confirmed": confirmed})


# ---------- transactions ----------

@v2_bp.route("/transactions")
@login_required
def transactions():
    """Every transaction in a month, each with a category picker."""
    today = date.today()
    try:
        y = int(request.args.get("y", today.year))
        m = int(request.args.get("m", today.month))
        date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    txns = (Transaction.query
            .filter(Transaction.date >= first, Transaction.date < nxt, Transaction.parent_id.is_(None))
            .order_by(Transaction.date.desc(), Transaction.id.desc()).all())
    cats = {c.id: c for c in Category.query}
    accounts = {a.id: a.name for a in Account.query}
    split_parents = {t.parent_id for t in Transaction.query.filter(Transaction.parent_id.isnot(None))}
    rows = []
    for t in txns:
        kind = "transfer" if t.transfer_id else ("split" if t.id in split_parents else "")
        rows.append({"t": t, "kind": kind, "cat": cats.get(t.category_id), "account": accounts.get(t.account_id, "")})
    prev = (y - 1, 12) if m == 1 else (y, m - 1)
    return render_template("v2/transactions.html", rows=rows, y=y, m=m,
                           month_label=first.strftime("%B %Y"), prev=prev, nxt=(nxt.year, nxt.month),
                           is_current=(y, m) == (today.year, today.month),
                           category_groups=_category_options(), cat_names={c.id: c.name for c in cats.values()})


@v2_bp.route("/transactions/<int:tid>/category", methods=["POST"])
@login_required
def transaction_category(tid):
    """Body: {"category_id": int|null}. Files one transaction under a category."""
    t = db.session.get(Transaction, tid)
    if not t:
        abort(404)
    if t.transfer_id or t.splits:
        return jsonify({"error": "Transfers and split transactions can't be recategorized here."}), 400
    cid = (request.get_json(silent=True) or {}).get("category_id")
    if cid is not None:
        cat = db.session.get(Category, cid)
        if not cat or cat.hidden:
            return jsonify({"error": "Pick an active category."}), 400
    old = t.category_id
    t.category_id = cid
    AutoCat.query.filter_by(transaction_id=t.id).delete()
    db.session.commit()
    return jsonify({"ok": True, "id": t.id, "from": old, "to": cid, "amount": t.amount})


# ---------- manage: payees & categories ----------

def _categorizable():
    """Transactions that carry their own category: not transfers, not split
    parents (their line-items carry the categories)."""
    return (Transaction.query
            .filter(Transaction.transfer_id.is_(None))
            .filter(~Transaction.splits.any()))


@v2_bp.route("/manage")
@login_required
def manage():
    cats = Category.query.order_by(Category.is_income.desc(), Category.group_name, Category.name).all()
    cat_by_id = {c.id: c for c in cats}
    year_ago = date.today() - timedelta(days=365)

    payees = {}
    for t in _categorizable().filter(Transaction.payee.isnot(None), Transaction.payee != ""):
        p = payees.setdefault(t.payee, {"payee": t.payee, "count": 0, "year": 0,
                                        "latest": t.date, "cats": defaultdict(int)})
        p["count"] += 1
        p["latest"] = max(p["latest"], t.date)
        if t.date >= year_ago:
            p["year"] += t.amount
        p["cats"][t.category_id] += 1

    rules = PayeeRule.query.all()
    rule_cat = {}
    for r in rules:
        rule_cat.setdefault(r.payee, r.category_id)
    for p in payees.values():
        ranked = sorted(p["cats"].items(), key=lambda kv: -kv[1])
        p["category_id"] = ranked[0][0]
        p["mixed"] = len(ranked) > 1
        p["breakdown"] = ", ".join(
            f"{n} in {cat_by_id[cid].name if cid in cat_by_id else 'uncategorized'}" for cid, n in ranked)
        p["rule"] = p["payee"] in rule_cat
        p["rule_cat"] = rule_cat.get(p["payee"])
        del p["cats"]

    groups = defaultdict(list)
    for c in cats:
        groups[c.group_name].append(c)
    usage = dict(db.session.query(Transaction.category_id, func.count())
                 .group_by(Transaction.category_id).all())
    return render_template(
        "v2/manage.html",
        payees=sorted(payees.values(), key=lambda p: (-p["count"], p["payee"].lower())),
        groups=sorted(groups.items(), key=lambda kv: (kv[0] != "Income", kv[0].lower())),
        usage=usage, category_groups=_category_options(),
        cat_names={c.id: c.name for c in cats},
        active_ids={c.id for c in cats if not c.hidden})


def file_payee(payee: str, cat: "Category | None", new_payee: str | None = None,
               history: bool = True) -> dict:
    """Rename a payee everywhere and/or file it under a category: every past
    transaction (when history is true) plus a PayeeRule for future imports.
    Caller commits."""
    new_payee = (new_payee or "").strip() or payee
    renamed = refiled = 0
    if new_payee != payee:
        renamed = (Transaction.query.filter(Transaction.payee == payee)
                   .update({"payee": new_payee}, synchronize_session=False))
        PayeeRule.query.filter(PayeeRule.payee == payee).update({"payee": new_payee}, synchronize_session=False)

    if cat:
        if history:
            q = _categorizable().filter(Transaction.payee == new_payee)
            if cat.is_income:  # same as rules: income categories only take deposits
                q = q.filter(Transaction.amount > 0)
            for t in q:
                if t.category_id != cat.id:
                    t.category_id = cat.id
                    refiled += 1
                AutoCat.query.filter_by(transaction_id=t.id).delete()
        # Point every rule that produces this payee at the category, and make
        # sure the clean name itself matches (bank strings vary).
        PayeeRule.query.filter(PayeeRule.payee == new_payee).update(
            {"category_id": cat.id}, synchronize_session=False)
        rule = PayeeRule.query.filter_by(match=_key(new_payee)).first()
        if not rule:
            db.session.add(PayeeRule(match=_key(new_payee), payee=new_payee, category_id=cat.id))
        else:
            rule.payee, rule.category_id = new_payee, cat.id
    return {"renamed": renamed, "refiled": refiled}


def save_category(cat: "Category | None", name: str, group: str, hidden=None, is_income=None) -> "Category":
    """Create (cat=None) or update a category. Raises ValueError on a bad or
    clashing name. Caller commits."""
    name, group = (name or "").strip(), (group or "").strip()
    if not name or not group:
        raise ValueError("A category needs a name and a group.")
    cat = cat or Category()
    clash = (Category.query.filter(func.lower(Category.name) == name.lower(),
                                   func.lower(Category.group_name) == group.lower(),
                                   Category.id != (cat.id or 0)).first())
    if clash:
        raise ValueError(f"{group} already has a category called {clash.name}.")
    cat.name, cat.group_name = name, group
    if hidden is not None:
        cat.hidden = bool(hidden)
    if is_income is not None:
        cat.is_income = bool(is_income)
    db.session.add(cat)
    db.session.flush()
    return cat


def set_budget(cat: "Category", cents: int, this_month_only: bool = False, y: int = None, m: int = None) -> None:
    """Budget from month (y, m) onward (default: this month), carrying forward;
    or for that month only, with the next month going back to what it was.
    Caller commits."""
    today = date.today()
    y, m = (y, m) if y and m else (today.year, today.month)
    if cat.is_income:
        plan = db.session.get(IncomePlan, cat.id) or IncomePlan(category_id=cat.id)
        plan.amount = cents
        db.session.add(plan)
        return
    ny, nm = (y + (m == 12), m % 12 + 1)
    prior = _budgets_as_of(y, m, [cat.id])[0].get(cat.id, 0)
    row = (BudgetMonth.query.filter_by(year=y, month=m, category_id=cat.id).first()
           or BudgetMonth(year=y, month=m, category_id=cat.id))
    row.assigned = cents
    db.session.add(row)
    if this_month_only:
        nxt = BudgetMonth.query.filter_by(year=ny, month=nm, category_id=cat.id).first()
        if not nxt:
            db.session.add(BudgetMonth(year=ny, month=nm, category_id=cat.id, assigned=prior))
    else:
        # Drop budgets already set for later months so this one carries forward.
        (BudgetMonth.query
         .filter(BudgetMonth.category_id == cat.id,
                 (BudgetMonth.year * 12 + BudgetMonth.month) > y * 12 + m)
         .delete(synchronize_session=False))


@v2_bp.route("/manage/payee", methods=["POST"])
@login_required
def manage_payee():
    """Body: {"payee": str, "new_payee": str?, "category_id": int?, "history": bool}."""
    data = request.get_json(silent=True) or {}
    payee = (data.get("payee") or "").strip()
    if not payee:
        return jsonify({"error": "No payee given."}), 400
    cat = db.session.get(Category, data["category_id"]) if data.get("category_id") else None
    if data.get("category_id") and (not cat or cat.hidden):
        return jsonify({"error": "Pick an active category."}), 400
    result = file_payee(payee, cat, data.get("new_payee"), data.get("history", True))
    db.session.commit()
    return jsonify({"ok": True, **result})


@v2_bp.route("/manage/payee/forget", methods=["POST"])
@login_required
def manage_payee_forget():
    """Body: {"payee": str}. Drops the rules for a payee; its transactions stay as they are."""
    payee = ((request.get_json(silent=True) or {}).get("payee") or "").strip()
    n = PayeeRule.query.filter(PayeeRule.payee == payee).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True, "removed": n})


@v2_bp.route("/manage/category", methods=["POST"])
@login_required
def manage_category():
    """Body: {"id": int?, "name": str, "group_name": str, "hidden": bool?, "is_income": bool?}.
    Creates the category when there's no id, otherwise updates it."""
    data = request.get_json(silent=True) or {}
    cat = None
    if data.get("id"):
        cat = db.session.get(Category, data["id"])
        if not cat:
            abort(404)
    try:
        cat = save_category(cat, data.get("name"), data.get("group_name"),
                            data.get("hidden"), data.get("is_income"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify({"ok": True, "id": cat.id})


@v2_bp.route("/manage/group", methods=["POST"])
@login_required
def manage_group():
    """Body: {"old": str, "new": str}. Renames a group (merging into an existing one of that name)."""
    data = request.get_json(silent=True) or {}
    old, new = (data.get("old") or "").strip(), (data.get("new") or "").strip()
    if not old or not new:
        return jsonify({"error": "A group needs a name."}), 400
    taken = {(c.name.lower(), c.group_name.lower()) for c in Category.query.filter(Category.group_name != old)}
    moving = Category.query.filter_by(group_name=old).all()
    dupes = [c.name for c in moving if (c.name.lower(), new.lower()) in taken]
    if dupes:
        return jsonify({"error": f"{new} already has: {', '.join(dupes)}."}), 400
    for c in moving:
        c.group_name = new
    db.session.commit()
    return jsonify({"ok": True, "moved": len(moving)})


# ---------- import ----------

def _add_transaction(rules, **fields) -> tuple[Transaction, bool]:
    t = Transaction(**fields)
    rule = _find_rule(rules, t.payee)
    if rule:
        _apply_rule(t, rule)
    db.session.add(t)
    return t, bool(rule and t.category_id)


@v2_bp.route("/import")
@login_required
def import_page():
    items = PlaidItem.query.order_by(PlaidItem.id).all()
    last_sync = {}
    for item in items:
        acct_ids = [a.id for a in Account.query.filter_by(plaid_item_id=item.item_id)]
        last_sync[item.id] = (db.session.query(func.max(Transaction.created_at))
                              .filter(Transaction.account_id.in_(acct_ids),
                                      Transaction.source == "plaid").scalar()) if acct_ids else None
    return render_template(
        "v2/import.html", items=items, last_sync=last_sync,
        plaid_ready=plaid_client.is_configured(),
        accounts=Account.query.filter_by(closed=False).order_by(Account.name).all(),
        autorun=request.args.get("ai") == "1")


@v2_bp.route("/import/sync", methods=["POST"])
@login_required
def import_sync():
    """Pull new transactions from every linked bank via Plaid, applying saved
    payee rules on the way in. AI categorization runs afterwards in batches
    (see review_ai) so this request stays fast."""
    if not plaid_client.is_configured():
        return jsonify({"error": "Plaid isn't configured on the server."}), 400
    rules = _rules_by_match()
    existing = {x[0] for x in db.session.query(Transaction.external_id)
                .filter(Transaction.external_id.isnot(None))}
    added = by_rule = 0
    errors = []
    for item in PlaidItem.query.all():
        try:
            result = plaid_client.sync_transactions(item.access_token, item.cursor)
        except Exception as exc:
            errors.append(f"{item.institution_name or 'Bank'}: {exc}")
            continue
        account_map = {a.plaid_account_id: a.id for a in
                       Account.query.filter(Account.plaid_item_id == item.item_id)}
        for t in result["added"]:
            # Skip pending: once it posts, Plaid sends it again as a new
            # transaction, so importing pending ones creates duplicates.
            if t.get("pending"):
                continue
            acct = account_map.get(t["account_id"])
            if not acct or t["transaction_id"] in existing:
                continue
            _, ruled = _add_transaction(
                rules, account_id=acct, date=t["date"],
                amount=-int(round(float(t["amount"]) * 100)),
                payee=t.get("name") or t.get("merchant_name") or "(bank)",
                memo="", source="plaid", external_id=t["transaction_id"])
            existing.add(t["transaction_id"])
            added += 1
            by_rule += ruled
        item.cursor = result["next_cursor"]
        db.session.commit()
    return jsonify({"added": added, "by_rule": by_rule, "errors": errors,
                    "to_categorize": len(_uncategorized_groups())})


@v2_bp.route("/import/csv", methods=["POST"])
@login_required
def import_csv():
    account = db.session.get(Account, request.form.get("account_id", type=int) or 0)
    f = request.files.get("file")
    if not account or not f:
        flash("Pick an account and a CSV file.", "error")
        return redirect(url_for("v2.import_page"))

    rows, _ = csv_import.parse_csv(f)
    if not rows:
        flash("Couldn't read any transactions from that file. Is it a bank CSV export?", "error")
        return redirect(url_for("v2.import_page"))

    # Same de-dupe key as v1, so importing a file in either version is safe.
    existing = {x[0] for x in db.session.query(Transaction.external_id)
                .filter(Transaction.external_id.isnot(None)).all()}
    rules = _rules_by_match()
    added = auto = 0
    for r in rows:
        if r["hash"] in existing:
            continue
        _, ruled = _add_transaction(rules, account_id=account.id, date=r["date"], amount=r["amount"],
                                    payee=r["payee"] or "(imported)", memo=r["memo"],
                                    source="csv", external_id=r["hash"])
        existing.add(r["hash"])
        added += 1
        auto += ruled
    db.session.commit()

    skipped = len(rows) - added
    msg = f"Imported {added} transaction{'s' if added != 1 else ''} into {account.name}"
    if auto:
        msg += f" — {auto} filed from your saved payees"
    if skipped:
        msg += f" ({skipped} already imported)"
    flash(msg + ".", "ok")
    return redirect(url_for("v2.import_page", ai=1))


# Routes for the Manage page's "Ask Claude" box (imports helpers above).
from v2 import assistant  # noqa: E402,F401
