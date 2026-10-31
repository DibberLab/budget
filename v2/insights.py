"""
"Worth knowing" card on Home: a critical read of the month's plan against the
household's own history - red flags, things to watch, ideas.

The numbers are computed here from the data (plan vs each category's typical
month, recent months' income and spending, upcoming three-paycheck months,
bill trends, uncategorized spending, late bills). Claude only turns those
facts into a handful of short, specific notes; it's told to use no number
that isn't in the facts. If Claude can't be reached, the same facts become
plain rule-based notes, so the card never comes up empty.

Notes are cached per month in a JSON file next to the database and rebuilt
when the facts change (at most every few hours) or on Refresh, so opening
Home doesn't cost a Claude call each time.
"""
import hashlib
import json
import os
import time
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta

from flask import current_app, jsonify, request
from flask_login import login_required
from sqlalchemy import extract, func

from models import Category, Transaction, db
from v2 import (IncomePlan, _bill_rhythm, _budgets_as_of, _chart_history, _late_bills, _month_add,
                _typical_averages, v2_bp)
from v2.assistant import MODEL, _claude

MAX_AGE = 6 * 3600        # rebuild changed facts at most this often
MIN_REFRESH = 120         # seconds between forced refreshes
KINDS = ("flag", "watch", "idea", "good")

# What the household has told us that the data alone doesn't say.
CONTEXT = """- Two-person household (Andy and Libby) in Wisconsin, with dogs (the "Lenny & Doug" category).
- Traded the Honda CR-V for a Honda Ridgeline (payment every two weeks).
- Took a $44,000 Amex loan to pay off the van and fund a well/laundry-room project, so van payments ended and "Amex Loan" is new.
- Capital One credit cards are paid off (CC Debt budgeted $0 going forward).
- All insurance except the boat is now one bill; the boat is with Progressive.
- "Extra $$ (Crosshatch)" on the sheet is their name for money left over at month end (a coffee-shop joke)."""

SYSTEM = """You review a household's monthly budget like a sharp, practical financial planner who knows their numbers. The household has the month's budget open and wants the few things most worth knowing right now: red flags, things to watch, and concrete ideas. Not generic advice.

Rules:
- Use only numbers that appear in the facts, rounded to whole dollars. Never invent or estimate a figure that isn't there. You may add or subtract figures from the facts (say what you combined).
- "Typical month" values exclude unusual months, and for bills paid on a steady schedule they are the usual payment. Trust them over single months.
- Be specific: name categories and amounts. Each note should change what they'd do or watch for.
- Prioritize: a plan that can't hold (typical spending well above budget), spending above income, rising bills, upcoming three-paycheck months to plan for, seasonal costs ahead for their month and location, and money left unassigned. Mention what's working only if it's notable.
- Don't flag things the context explains (e.g. CC Debt dropping to $0 because the cards are paid off; Amex Loan having no history because it's new).
- Don't nag about things that are fine, and don't repeat the same point twice.

Write 4 to 6 notes with the insights tool, most important first, plus a one-sentence headline that sums up where the budget stands."""

TOOL = {
    "name": "insights",
    "description": "Show the notes on the household's budget page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "One sentence, under 25 words: where the budget stands."},
            "notes": {
                "type": "array", "minItems": 3, "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(KINDS),
                                 "description": "flag = red flag, watch = keep an eye on, idea = suggestion, good = working well"},
                        "title": {"type": "string", "description": "Under 8 words."},
                        "body": {"type": "string", "description": "1-2 sentences, under 45 words, with the specific numbers."},
                    },
                    "required": ["kind", "title", "body"],
                },
            },
        },
        "required": ["headline", "notes"],
    },
}


def _d(cents) -> int:
    return round(abs(cents) / 100)


def _paydays(cid, today):
    """Three-paycheck months in the next year for an income paid every two weeks."""
    rows = (db.session.query(Transaction.date, Transaction.amount)
            .filter(Transaction.category_id == cid, Transaction.amount > 0,
                    Transaction.date >= today - timedelta(days=120))
            .order_by(Transaction.date).all())
    r = _bill_rhythm([(d, a) for d, a in rows])
    if not r or r["rhythm"] != "every two weeks":
        return None
    d, horizon, per = r["events"][-1][0], today + timedelta(days=366), defaultdict(int)
    while d <= horizon:
        d += timedelta(days=r["gap"])
        if d > today:
            per[(d.year, d.month)] += 1
    return {"paycheck": _d(r["amount"]), "three_paycheck_months": [date(*ym, 1).strftime("%B %Y") for ym, n in sorted(per.items()) if n >= 3]}


def build_facts(y: int, m: int) -> dict:
    today = date.today()
    is_current, is_future = (y, m) == (today.year, today.month), (y, m) > (today.year, today.month)
    cats = Category.query.order_by(Category.group_name, Category.name).all()
    live = [c for c in cats if not c.hidden]
    ch = _chart_history(y, m, cats)
    everyday = set(ch["everyday"])
    avg, _, detail, months = _typical_averages(y, m, cats, everyday, today)
    plan, _ = _budgets_as_of(y, m, [c.id for c in live if not c.is_income])
    inc_plan = {p.category_id: p.amount for p in IncomePlan.query}
    spent = {cid: int(t or 0) for cid, t in
             db.session.query(Transaction.category_id, func.sum(Transaction.amount))
             .filter(extract("year", Transaction.date) == y, extract("month", Transaction.date) == m)
             .group_by(Transaction.category_id)}

    categories, groups = [], defaultdict(lambda: {"plan": 0, "typical": 0})
    for c in live:
        if c.is_income:
            continue
        d = detail.get(c.id) or {}
        row = {"group": c.group_name, "name": c.name, "budget": _d(plan.get(c.id, 0)),
               "typical_month": _d(avg.get(c.id, 0)), "everyday": c.id in everyday}
        if d.get("how") == "bill":
            row["paid"] = d["rhythm"]
            row["recent_payments"] = [_d(a) for _, a, _ in d["months"]]
        elif d.get("months"):
            row["by_month"] = {lbl: _d(v) for lbl, v, _ in d["months"]}
        if not is_future:
            row["spent_this_month"] = _d(-spent.get(c.id, 0)) if spent.get(c.id, 0) < 0 else 0
        categories.append(row)
        groups[c.group_name]["plan"] += row["budget"]
        groups[c.group_name]["typical"] += row["typical_month"]

    income = []
    for c in live:
        if not c.is_income:
            continue
        i = {"name": c.name, "planned": _d(inc_plan.get(c.id, 0)), "typical_month": _d(avg.get(c.id, 0))}
        if not is_future:
            i["received_this_month"] = _d(spent.get(c.id, 0))
        pay = _paydays(c.id, today)
        if pay:
            i.update(pay)
        income.append(i)

    keys = [_month_add(y, m, k) for k in range(-5, 1)]
    recent = [{"month": h["full"], "income": _d(h["income"]), "spent": _d(h["spent"]), "budget": _d(h["budget"]),
               "complete_month": ym in months}
              for ym, h in zip(keys, ch["history"]) if ym < (today.year, today.month) and (h["spent"] or h["income"])]

    unc = (db.session.query(func.count(), func.sum(Transaction.amount))
           .filter(Transaction.category_id.is_(None), Transaction.date >= today - timedelta(days=60)).one())
    retired = [{"name": c.name, "spent_this_month": _d(spent[c.id])} for c in cats
               if c.hidden and not c.is_income and spent.get(c.id, 0) < 0]

    planned_spend = sum(r["budget"] for r in categories)
    planned_income = sum(i["planned"] for i in income)
    facts = {
        "month": date(y, m, 1).strftime("%B %Y"),
        "status": "in progress" if is_current else ("being planned (future)" if is_future else "finished"),
        "today": today.isoformat(),
        "plan": {"income": planned_income, "spending": planned_spend, "left_over": planned_income - planned_spend},
        "typical_month_totals": {"income": sum(i["typical_month"] for i in income),
                                 "spending": sum(r["typical_month"] for r in categories)},
        "typical_months_used": [date(*ym, 1).strftime("%b %Y") for ym in months],
        "groups": {g: v for g, v in sorted(groups.items())},
        "categories": categories,
        "income": income,
        "recent_months": recent,
        "uncategorized_last_60_days": {"count": unc[0], "amount": _d(unc[1] or 0)},
    }
    if is_current:
        days = monthrange(y, m)[1]
        facts["days_left_in_month"] = days - today.day
        facts["late_bills"] = [{"name": next(c.name for c in cats if c.id == lb["id"]), "usual": _d(lb["amount"]),
                                "expected": lb["expected"], "days_late": lb["days_late"]}
                               for lb in _late_bills(today, everyday)]
    if retired:
        facts["retired_categories_with_spending"] = retired
    return facts


def rule_notes(f: dict) -> dict:
    """Plain notes straight from the facts, for when Claude isn't available."""
    notes = []
    p = f["plan"]
    if p["left_over"] < 0:
        notes.append({"kind": "flag", "title": "Budget is more than income",
                      "body": f"The plan spends ${p['spending']:,} against ${p['income']:,} of planned income, ${-p['left_over']:,} short."})
    # A $0 budget on a category with history means it was stopped on purpose (a paid-off card, a cancelled bill).
    under = sorted((r for r in f["categories"] if r["budget"] and r["typical_month"] - r["budget"] >= 40
                    and r["typical_month"] > r["budget"] * 1.15), key=lambda r: r["budget"] - r["typical_month"])
    for r in under[:3]:
        notes.append({"kind": "watch", "title": f"{r['name']} usually runs higher",
                      "body": f"Budgeted ${r['budget']:,}, but a typical month is ${r['typical_month']:,}."})
    for i in f["income"]:
        if i.get("three_paycheck_months"):
            notes.append({"kind": "idea", "title": f"Extra paycheck coming ({i['name']})",
                          "body": f"Three paychecks land in {', '.join(i['three_paycheck_months'][:2])}. Decide now where that extra ${i['paycheck']:,} goes."})
    for lb in f.get("late_bills", []):
        notes.append({"kind": "flag", "title": f"{lb['name']} hasn't shown up",
                      "body": f"Usually about ${lb['usual']:,}; expected around {lb['expected']}."})
    u = f["uncategorized_last_60_days"]
    if u["count"]:
        notes.append({"kind": "watch", "title": "Uncategorized spending",
                      "body": f"{u['count']} transactions (${u['amount']:,}) in the last 60 days aren't in a category yet."})
    if not notes:
        notes.append({"kind": "good", "title": "Plan matches your history",
                      "body": "Every category's budget is close to what a typical month costs."})
    head = (f"The plan leaves ${p['left_over']:,} over." if p["left_over"] >= 0
            else f"The plan is ${-p['left_over']:,} more than planned income.")
    return {"headline": head, "notes": notes[:6]}


def ask_claude(f: dict) -> dict:
    resp = _claude().beta.messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM, tools=[TOOL],
        tool_choice={"type": "tool", "name": "insights"},
        messages=[{"role": "user", "content": "Known context from the household:\n" + CONTEXT
                   + "\n\nFacts (amounts in whole dollars):\n" + json.dumps(f, indent=1)}],
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    out = next(b.input for b in resp.content if b.type == "tool_use")
    notes = [n for n in out.get("notes", []) if n.get("kind") in KINDS and n.get("title") and n.get("body")]
    if not notes:
        raise ValueError("no notes")
    return {"headline": str(out.get("headline", "")), "notes": notes[:6]}


def _cache_path(y, m):
    base = os.path.dirname(db.engine.url.database or "") or "."
    path = os.path.join(base, "insights")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, f"{y}-{m:02d}.json")


@v2_bp.route("/insights")
@login_required
def insights():
    today = date.today()
    y, m = int(request.args.get("y", today.year)), int(request.args.get("m", today.month))
    refresh = request.args.get("refresh") == "1"
    facts = build_facts(y, m)
    digest = hashlib.sha256(json.dumps({k: v for k, v in facts.items() if k != "today"}, sort_keys=True).encode()).hexdigest()
    path, cached = _cache_path(y, m), None
    try:
        with open(path) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass
    if cached and cached.get("source") == "claude":
        age = time.time() - cached["at"]
        if (not refresh and (cached["hash"] == digest or age < MAX_AGE)) or (refresh and age < MIN_REFRESH):
            return jsonify({**cached["result"], "source": "claude", "at": cached["at"], "stale": cached["hash"] != digest})
    try:
        result, source = ask_claude(facts), "claude"
    except Exception as exc:  # anything from the API: fall back, never an empty card
        current_app.logger.warning("insights: Claude call failed: %r", exc)
        result, source = rule_notes(facts), "rules"
        result["note"] = "Claude couldn't be reached, so these are the basic checks."
    if source == "claude":
        try:
            with open(path + ".tmp", "w") as fh:
                json.dump({"hash": digest, "at": time.time(), "result": result, "source": source}, fh)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
    return jsonify({**result, "source": source, "at": time.time(), "stale": False})
