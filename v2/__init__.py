"""
Budget v2 — a deliberately small front-end over the same database as v1.

Three screens, mounted at /v2:
  /v2/          Progress: this month's spending vs. budget, one bar per category.
  /v2/review    Uncategorized transactions, grouped by payee. Clean up the payee
                name, pick a category, save — the choice is remembered as a
                PayeeRule so future imports of that payee are handled automatically.
  /v2/import    Upload a bank CSV. Rules are applied on the way in.

v2 shares login, users, accounts, categories, budgets, and transactions with v1,
so both versions can run side by side until v1 is retired.
"""
from calendar import monthrange
from collections import defaultdict
from datetime import date

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import login_required
from sqlalchemy import extract, func

import csv_import
from models import Account, BudgetMonth, Category, Transaction, db

v2_bp = Blueprint("v2", __name__, url_prefix="/v2", template_folder="templates")


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


def _apply_rule(t: Transaction, rule: "PayeeRule") -> None:
    t.payee = rule.payee
    if rule.category_id and t.category_id is None:
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


@v2_bp.app_context_processor
def _inject_counts():
    # Only the v2 templates use this; cheap enough to compute per request.
    if request.blueprint != "v2":
        return {}
    return {"v2_uncategorized": _uncategorized_query().count()}


# ---------- progress ----------

@v2_bp.route("/")
@login_required
def progress():
    today = date.today()
    try:
        y = int(request.args.get("y", today.year))
        m = int(request.args.get("m", today.month))
        date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month

    cats = Category.query.filter_by(hidden=False, is_income=False).all()
    cat_ids = [c.id for c in cats]

    spent_rows = (db.session.query(Transaction.category_id,
                                   func.coalesce(func.sum(Transaction.amount), 0))
                  .filter(Transaction.category_id.in_(cat_ids),
                          extract("year", Transaction.date) == y,
                          extract("month", Transaction.date) == m)
                  .group_by(Transaction.category_id).all())
    spent = {cid: -int(total) for cid, total in spent_rows}  # outflow as positive

    # Budget = this month's assignment. If a category has nothing assigned this
    # month, fall back to its most recent earlier assignment so a budget that
    # was set once keeps working without re-entering it every month.
    ym = y * 12 + m
    budget_rows = (BudgetMonth.query
                   .filter(BudgetMonth.category_id.in_(cat_ids),
                           (BudgetMonth.year * 12 + BudgetMonth.month) <= ym,
                           BudgetMonth.assigned > 0)
                   .order_by(BudgetMonth.year, BudgetMonth.month).all())
    budget, budget_from = {}, {}
    for b in budget_rows:  # ascending, so the latest wins
        budget[b.category_id] = b.assigned
        budget_from[b.category_id] = (b.year, b.month)

    rows, unbudgeted = [], []
    for c in cats:
        s, b = spent.get(c.id, 0), budget.get(c.id, 0)
        if b:
            carried = budget_from[c.id] != (y, m)
            rows.append({"cat": c, "spent": s, "budget": b, "left": b - s,
                         "pct": round(100 * s / b) if b else 0, "carried": carried})
        elif s > 0:
            unbudgeted.append({"cat": c, "spent": s})
    rows.sort(key=lambda r: -r["pct"])
    unbudgeted.sort(key=lambda r: -r["spent"])

    days_in_month = monthrange(y, m)[1]
    if (y, m) == (today.year, today.month):
        pace = round(100 * today.day / days_in_month)
    elif (y, m) < (today.year, today.month):
        pace = 100
    else:
        pace = 0

    income = (db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
              .join(Category, Category.id == Transaction.category_id)
              .filter(Category.is_income.is_(True),
                      extract("year", Transaction.date) == y,
                      extract("month", Transaction.date) == m).scalar() or 0)

    total_budget = sum(r["budget"] for r in rows)
    total_spent_budgeted = sum(r["spent"] for r in rows)
    total_unbudgeted = sum(r["spent"] for r in unbudgeted)

    prev = (y - 1, 12) if m == 1 else (y, m - 1)
    nxt = (y + 1, 1) if m == 12 else (y, m + 1)

    return render_template(
        "v2/progress.html",
        y=y, m=m, month_label=date(y, m, 1).strftime("%B %Y"),
        prev=prev, nxt=nxt, is_current=(y, m) == (today.year, today.month),
        rows=rows, unbudgeted=unbudgeted, pace=pace, income=int(income),
        total_budget=total_budget, total_spent_budgeted=total_spent_budgeted,
        total_unbudgeted=total_unbudgeted,
        total_pct=round(100 * total_spent_budgeted / total_budget) if total_budget else 0,
    )


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
    return jsonify([{"date": t.date.strftime("%b %-d"), "payee": t.payee,
                     "amount": -t.amount} for t in txns])


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
        rule = rules.get(k)
        g["suggest_payee"] = rule.payee if rule else g["raw"]
        g["suggest_cat"] = (rule.category_id if rule and rule.category_id
                            else history.get(g["raw"]))
        g["key"] = k

    ordered = sorted(groups.values(), key=lambda g: (-len(g["txns"]), g["raw"].lower()))
    return render_template("v2/review.html", groups=ordered,
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


@v2_bp.route("/review/ai", methods=["POST"])
@login_required
def review_ai():
    """Ask Claude for a clean name + category for each uncategorized payee group.
    Returns suggestions only; nothing is saved until the user hits Save."""
    from app import _ai_tool_call  # late import: app.py imports this module

    txns = _uncategorized_query().order_by(Transaction.date.desc()).limit(400).all()
    if not txns:
        return jsonify({"suggestions": []})

    groups = {}
    for t in txns:
        g = groups.setdefault(_key(t.payee), {"raw": t.payee, "n": 0, "total": 0, "memo": ""})
        g["n"] += 1
        g["total"] += t.amount
        g["memo"] = g["memo"] or (t.memo or "")
    groups = dict(list(groups.items())[:150])

    cats = Category.query.filter_by(hidden=False).order_by(Category.group_name, Category.name).all()
    lookup = {(c.group_name.strip().lower(), c.name.strip().lower()): c.id for c in cats}
    cat_text = "\n".join(f"  {c.group_name} > {c.name}" + (" (income)" if c.is_income else "")
                         for c in cats)

    known = (db.session.query(Transaction.payee, Category.group_name, Category.name)
             .join(Category, Category.id == Transaction.category_id)
             .group_by(Transaction.payee, Category.group_name, Category.name)
             .order_by(func.count().desc()).limit(200).all())
    known_text = "\n".join(f"  {p} → {g} > {n}" for p, g, n in known)

    payee_text = "\n".join(
        f"  {g['raw']} | {g['n']} txn | {'+' if g['total'] > 0 else '-'}${abs(g['total'])/100:,.2f}"
        + (f" | memo: {g['memo'][:60]}" if g["memo"] else "")
        for g in groups.values())

    prompt = (
        "These are raw payee strings from bank/credit-card transactions that still need "
        "a category. For each one, give a short clean human-readable payee name "
        "(e.g. 'AMZN MKTP US*AB12CD3' → 'Amazon', 'SQ *JOES COFFEE 0423' → \"Joe's Coffee\") "
        "and the best category from the list. Reuse the exact clean names from the "
        "already-categorized examples when it's the same merchant. Negative = spending, "
        "positive = income. Leave category empty if genuinely unclear.\n\n"
        f"## Categories\n{cat_text}\n\n"
        + (f"## Already-categorized examples\n{known_text}\n\n" if known_text else "")
        + f"## Payees to clean up\n{payee_text}"
    )

    try:
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
                    },
                    "required": ["raw", "payee", "category"],
                }}},
                "required": ["suggestions"],
            },
            max_tokens=16384,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    out = []
    for s in parsed.get("suggestions", []):
        k = _key(s.get("raw"))
        if k not in groups:
            continue
        cid = None
        if ">" in (s.get("category") or ""):
            g, n = (p.strip().lower() for p in s["category"].split(">", 1))
            cid = lookup.get((g, n))
        out.append({"key": k, "payee": (s.get("payee") or "").strip(), "category_id": cid})
    return jsonify({"suggestions": out})


# ---------- import ----------

@v2_bp.route("/import", methods=["GET", "POST"])
@login_required
def import_csv():
    accounts = Account.query.filter_by(closed=False).order_by(Account.name).all()
    if request.method == "GET":
        return render_template("v2/import.html", accounts=accounts)

    account = db.session.get(Account, request.form.get("account_id", type=int) or 0)
    f = request.files.get("file")
    if not account or not f:
        flash("Pick an account and a CSV file.", "error")
        return redirect(url_for("v2.import_csv"))

    rows, _ = csv_import.parse_csv(f)
    if not rows:
        flash("Couldn't read any transactions from that file. Is it a bank CSV export?", "error")
        return redirect(url_for("v2.import_csv"))

    # Same de-dupe key as v1, so importing a file in either version is safe.
    existing = {x[0] for x in db.session.query(Transaction.external_id)
                .filter(Transaction.external_id.isnot(None)).all()}
    rules = _rules_by_match()
    added = auto = 0
    for r in rows:
        if r["hash"] in existing:
            continue
        t = Transaction(account_id=account.id, date=r["date"], amount=r["amount"],
                        payee=r["payee"] or "(imported)", memo=r["memo"],
                        source="csv", external_id=r["hash"])
        rule = rules.get(_key(t.payee))
        if rule:
            _apply_rule(t, rule)
            if t.category_id:
                auto += 1
        db.session.add(t)
        existing.add(r["hash"])
        added += 1
    db.session.commit()

    skipped = len(rows) - added
    msg = f"Imported {added} transaction{'s' if added != 1 else ''} into {account.name}"
    if auto:
        msg += f" — {auto} categorized automatically from your saved payees"
    if skipped:
        msg += f" ({skipped} already imported, skipped)"
    flash(msg + ".", "ok")
    return redirect(url_for("v2.review"))
