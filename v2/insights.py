"""
"Worth knowing" card on Home: a critical read of the month's plan against the
household's own history - red flags, things to watch, ideas.

The numbers are computed here from the data (plan vs each category's typical
month, recent months' income and spending, upcoming three-paycheck months,
bill trends, uncategorized spending, late bills). Claude only turns those
facts into a handful of short, specific notes; it's told to use no number
that isn't in the facts. If Claude can't be reached, the same facts become
plain rule-based notes, so the card never comes up empty.

The household can answer a note in the card's reply box ("the extra CC Loan
money is on purpose, we're paying it off by March"). Claude checks the reply
against the notes and facts: if it settles the point, it's saved as a short
household fact (HouseholdFact) that every later review takes into account,
and the card is rebuilt; if not, Claude asks a follow-up and nothing is saved.

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
from datetime import date, datetime, timedelta

from flask import current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import extract, func

from models import Category, Transaction, db
from v2 import (IncomePlan, _bill_rhythm, _budgets_as_of, _chart_history, _late_bills, _month_add,
                _typical_averages, v2_bp)
from v2.assistant import MODEL, _claude

class HouseholdFact(db.Model):
    """Something the household told the review, kept for every later review."""
    __tablename__ = "household_facts"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    author = db.Column(db.String(120), nullable=False, default="")
    month = db.Column(db.String(7), nullable=False, default="")   # "2026-11": the card it was answered on
    said = db.Column(db.Text, nullable=False)                     # their words
    fact = db.Column(db.Text, nullable=False)                     # Claude's one-line summary, used in reviews


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
- "What the household told us" answers earlier notes. Treat those points as settled: don't raise them again unless the facts now clearly contradict what they said (then say so plainly), and use them to look for what's next instead.

Always answer by calling the insights tool (no other text): 4 to 6 notes, most important first, plus a one-sentence headline that sums up where the budget stands."""

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


def _told() -> list[str]:
    return [f"{h.created_at:%b %-d, %Y} ({h.month}): {h.fact}" for h in HouseholdFact.query.order_by(HouseholdFact.id)]


def _context_text() -> str:
    told = _told()
    return ("Known context from the household:\n" + CONTEXT
            + ("\n\nWhat the household told us (answers to earlier notes):\n" + "\n".join("- " + t for t in told) if told else ""))


def ask_claude(f: dict) -> dict:
    resp = _claude().beta.messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM, tools=[TOOL],
        messages=[{"role": "user", "content": _context_text()
                   + "\n\nFacts (amounts in whole dollars):\n" + json.dumps(f, indent=1)}],
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    # This model doesn't accept a forced tool_choice; the system prompt asks for the tool instead.
    out = next((b.input for b in resp.content if b.type == "tool_use" and b.name == "insights"), None)
    if not isinstance(out, dict):
        raise ValueError("Claude answered without the insights tool")
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
    ctx = hashlib.sha256(_context_text().encode()).hexdigest()
    path, cached = _cache_path(y, m), None
    try:
        with open(path) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass
    if cached and cached.get("source") == "claude" and cached.get("ctx") == ctx:  # something new was told: rebuild
        age = time.time() - cached["at"]
        if (not refresh and (cached["hash"] == digest or age < MAX_AGE)) or (refresh and age < MIN_REFRESH):
            return jsonify({**cached["result"], "source": "claude", "at": cached["at"], "stale": cached["hash"] != digest})
    try:
        result, source = ask_claude(facts), "claude"
    except Exception as exc:  # anything from the API: fall back, never an empty card
        current_app.logger.warning("insights: Claude call failed: %r", exc)
        result, source = rule_notes(facts), "rules"
        result["note"] = "Claude's notes aren't available right now, so these are the basic checks."
    if source == "claude":
        try:
            with open(path + ".tmp", "w") as fh:
                json.dump({"hash": digest, "ctx": ctx, "at": time.time(), "result": result, "source": source}, fh)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
    return jsonify({**result, "source": source, "at": time.time(), "stale": False})


REPLY_SYSTEM = """You look after a household's budget review. The household is answering one or more of the review's notes in their own words. Decide whether the reply settles something, using the notes, the facts and what they've told you before.

- If it settles or explains a point (why a number is what it is, a decision they've made, a plan, a one-off cause), record it with the record tool: settled=true and a fact that is one clear sentence a future reviewer can rely on. Name categories and amounts, and include dates when a time limit applies, e.g. "CC Loan's $600 budget is deliberate: paying it off early, done by March 2027." Keep their decision, not your opinion of it.
- If it's too vague to rely on, contradicts the data in a way they should check, or doesn't relate to the budget, use settled=false and ask one short, specific follow-up question in reply.
- If they ask you to change something (a budget amount, a category), you can't do that from here. Say where to do it: the amount box on the sheet, or Manage > Ask Claude. Still record any explanation they gave.
- reply: one or two friendly sentences to them saying what you recorded (or what you need). No filler."""

RECORD_TOOL = {
    "name": "record",
    "description": "Record the outcome of the household's reply.",
    "input_schema": {
        "type": "object",
        "properties": {
            "settled": {"type": "boolean", "description": "True if the reply settles or explains a point well enough to rely on."},
            "fact": {"type": "string", "description": "When settled: one sentence for future reviews. Empty otherwise."},
            "reply": {"type": "string", "description": "What to tell the household (1-2 sentences)."},
        },
        "required": ["settled", "reply"],
    },
}


@v2_bp.route("/insights/reply", methods=["POST"])
@login_required
def insights_reply():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()[:2000]
    y, m = int(body.get("y", 0)), int(body.get("m", 0))
    if not text or not (1 <= m <= 12):
        return jsonify({"error": "Write a reply first."}), 400
    notes = body.get("notes") or []  # the notes on screen, so "the snow one" makes sense
    facts = build_facts(y, m)
    try:
        resp = _claude().beta.messages.create(
            model=MODEL, max_tokens=2000, system=REPLY_SYSTEM, tools=[RECORD_TOOL],
            messages=[{"role": "user", "content": _context_text()
                       + "\n\nThe review's notes they're looking at:\n" + json.dumps(notes[:8], indent=1)[:6000]
                       + "\n\nFacts (amounts in whole dollars):\n" + json.dumps(facts, indent=1)
                       + "\n\nTheir reply:\n" + text
                       + "\n\nAnswer only by calling the record tool."}],
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        out = next((b.input for b in resp.content if b.type == "tool_use" and b.name == "record"), None)
        if not isinstance(out, dict):
            raise ValueError("no record call")
    except Exception as exc:
        current_app.logger.warning("insights reply: Claude call failed: %r", exc)
        return jsonify({"error": "Claude couldn't read that just now. Try again in a minute."}), 502
    saved = None
    fact = str(out.get("fact") or "").strip()
    if out.get("settled") and fact:
        h = HouseholdFact(author=getattr(current_user, "display_name", "") or "", month=f"{y}-{m:02d}",
                          said=text, fact=fact[:500])
        db.session.add(h)
        db.session.commit()
        saved = {"id": h.id, "fact": h.fact}
    return jsonify({"settled": bool(saved), "reply": str(out.get("reply", "")), "saved": saved})


@v2_bp.route("/insights/facts")
@login_required
def insights_facts():
    return jsonify({"facts": [{"id": h.id, "fact": h.fact, "said": h.said, "author": h.author,
                               "when": h.created_at.strftime("%b %-d")}
                              for h in HouseholdFact.query.order_by(HouseholdFact.id.desc())]})


@v2_bp.route("/insights/facts/<int:fid>", methods=["DELETE"])
@login_required
def insights_fact_delete(fid):
    h = db.session.get(HouseholdFact, fid)
    if h:
        db.session.delete(h)
        db.session.commit()
    return jsonify({"ok": True})


# ---------- Reports: notes on whatever slice the Reports page is showing ----------
# The page sends the facts it computed for its current view (range, filters,
# comparison). They're the household's own numbers, already shown on screen;
# Claude only reads them. Same rules as Home: no invented figures, rule-based
# notes when Claude can't be reached, cached per view so revisiting is free.

REPORT_SYSTEM = """You read a household's spending report the way a sharp, practical financial planner would. They have a report open (a date range, maybe filtered to some groups, categories or payees, maybe compared with an earlier period) and want the few things most worth knowing about what's on screen. Not generic advice.

Rules:
- Use only numbers that appear in the facts, rounded to whole dollars. Never invent or estimate a figure. You may add or subtract figures from the facts (say what you combined).
- Talk about this view: its range, its filters, its comparison. If it's filtered, keep to what's filtered.
- Look for what changes a decision: the biggest drivers of the total, big moves against the comparison period (and which payees or categories caused them), new or growing payees, ones that stopped, one-off large charges that distort a total, timing patterns, money still uncategorized.
- A change against a short or unusual comparison period can mislead; say so if the facts suggest it.
- Don't flag things the household's context explains, and treat what they've told you as settled unless the facts clearly contradict it.
- Don't nag about things that are fine, and don't repeat a point.

Always answer by calling the insights tool (no other text): 3 to 6 notes, most important first, plus a one-sentence headline about this view."""

MAX_FACTS = 40_000  # characters; the page sends a compact summary, never raw transactions


def report_rule_notes(f: dict) -> dict:
    """Plain notes from the report facts, for when Claude isn't available."""
    notes, t, cmp = [], f.get("totals", {}), f.get("compare")
    label = f.get("view", {}).get("compare_label") or "the comparison period"
    spent = t.get("spent", 0)
    if cmp and cmp.get("spent") is not None and cmp["spent"]:
        diff = spent - cmp["spent"]
        if abs(diff) >= max(50, 0.1 * cmp["spent"]):
            notes.append({"kind": "watch" if diff > 0 else "good",
                          "title": f"Spending {'up' if diff > 0 else 'down'} ${abs(diff):,}",
                          "body": f"${spent:,} here against ${cmp['spent']:,} in {label}."})
    for m in (f.get("movers_up") or [])[:2]:
        notes.append({"kind": "watch", "title": f"{m['name']} rose ${m['change']:,}",
                      "body": f"${m['now']:,} here vs ${m['before']:,} in {label}."})
    groups = f.get("by_group") or f.get("by_category") or []
    if groups and spent and groups[0]["total"] >= 0.35 * spent:
        g = groups[0]
        notes.append({"kind": "idea", "title": f"{g['name']} is the big one",
                      "body": f"${g['total']:,} of ${spent:,} ({round(100 * g['total'] / spent)}%) in this view."})
    for p in (f.get("new_payees") or [])[:1]:
        notes.append({"kind": "watch", "title": f"New: {p['name']}", "body": f"${p['total']:,} here and nothing in {label}."})
    u = f.get("uncategorized") or {}
    if u.get("count"):
        notes.append({"kind": "watch", "title": "Some of this isn't categorized",
                      "body": f"{u['count']} transaction{'s' if u['count'] != 1 else ''} (${u['amount']:,}) {'are' if u['count'] != 1 else 'is'} uncategorized, so category totals are low by that much."})
    if not notes:
        notes.append({"kind": "good", "title": "Nothing unusual here", "body": "No big changes or outliers in this view."})
    view = f.get("view", {})
    head = f"${spent:,} spent and ${t.get('income', 0):,} in for {view.get('label') or view.get('range') or 'this range'}."
    return {"headline": head, "notes": notes[:6]}


@v2_bp.route("/reports/insights", methods=["POST"])
@login_required
def reports_insights():
    """Body: {"facts": {...}, "refresh": bool, "cached_only": bool}."""
    body = request.get_json(silent=True) or {}
    facts = body.get("facts")
    raw = json.dumps(facts, sort_keys=True) if isinstance(facts, dict) else ""
    if not raw or len(raw) > MAX_FACTS:
        return jsonify({"error": "Nothing to read."}), 400
    ctx = _context_text()
    digest = hashlib.sha256((raw + ctx).encode()).hexdigest()[:24]
    path = os.path.join(os.path.dirname(_cache_path(2000, 1)), f"reports-{digest}.json")
    cached = None
    try:
        with open(path) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass
    if cached and (not body.get("refresh") or time.time() - cached["at"] < MIN_REFRESH):
        return jsonify({**cached["result"], "source": "claude", "at": cached["at"], "cached": True})
    if body.get("cached_only"):
        return jsonify({"missing": True})
    try:
        resp = _claude().beta.messages.create(
            model=MODEL, max_tokens=3000, system=REPORT_SYSTEM, tools=[TOOL],
            messages=[{"role": "user", "content": ctx + "\n\nThe report on screen (amounts in whole dollars):\n" + json.dumps(facts, indent=1)}],
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        out = next((b.input for b in resp.content if b.type == "tool_use" and b.name == "insights"), None)
        notes = [n for n in (out or {}).get("notes", []) if n.get("kind") in KINDS and n.get("title") and n.get("body")]
        if not notes:
            raise ValueError("no notes")
        result, source = {"headline": str(out.get("headline", "")), "notes": notes[:6]}, "claude"
    except Exception as exc:  # never an empty card
        current_app.logger.warning("reports insights: Claude call failed: %r", exc)
        result, source = report_rule_notes(facts), "rules"
        result["note"] = "Claude's notes aren't available right now, so these are the basic checks."
    if source == "claude":
        try:
            with open(path + ".tmp", "w") as fh:
                json.dump({"at": time.time(), "result": result}, fh)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
    return jsonify({**result, "source": source, "at": time.time()})
