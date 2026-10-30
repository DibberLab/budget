"""
"Ask Claude" on the Manage page: describe a change in plain words ("we rolled
all the insurance into one Progressive policy") and Claude proposes the edits
to categories, payees, rules and budgets. Nothing changes until someone presses
Apply.

Each HTTP request runs a single Claude call (one step of the tool loop), so no
request outlives the 60s nginx/Cloudflare timeouts; the browser keeps calling
/manage/assistant/step until Claude is done. Conversation state lives in the
assistant_chats table between steps.

Lookup tools (find_payees, find_transactions) run immediately. Change tools only
record a validated proposal with a plain-English summary; /manage/assistant/apply
backs up the database and applies the selected proposals in one transaction,
from the server's stored copy (never from what the browser sends back).
"""
import json
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

import anthropic
from flask import abort, jsonify, request
from flask_login import login_required
from sqlalchemy import func

from models import Category, Transaction, db
from v2 import (PayeeRule, _budgets_as_of, _categorizable, file_payee, save_category,
                set_budget, v2_bp)

MODEL = "claude-opus-5-5"
MAX_STEPS = 12          # Claude calls per user message
KEEP_BACKUPS = 15       # assistant-made DB backups kept next to the database


class AssistantChat(db.Model):
    __tablename__ = "assistant_chats"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.Column(db.Text, nullable=False, default="[]")    # API messages, append-only
    transcript = db.Column(db.Text, nullable=False, default="[]")  # [{role, text}] for display
    proposals = db.Column(db.Text, nullable=False, default="[]")   # [{id, tool, input, summary, status}]
    steps = db.Column(db.Integer, nullable=False, default=0)       # steps since the last user message
    done = db.Column(db.Boolean, nullable=False, default=True)


SYSTEM = """You help a two-person household keep their budget app's setup accurate. They tell you what changed in their lives or finances ("we traded the CRV for a Ridgeline", "insurance is one bill now", "Claude and DigitalOcean are subscriptions") and you work out the matching changes to categories, payees, payee rules and monthly budgets.

How the app works:
- Categories sit in groups (e.g. Transportation > Gas). Retired categories are hidden from budgets and pickers but keep their history.
- A monthly budget carries forward until it's changed. set_budget changes it from this month on; with this_month_only it changes just this month (useful for a one-time payoff) and next month goes back to the previous amount.
- A payee rule files new bank imports from that payee into a category automatically. file_payee sets the rule and, with move_history, moves that payee's past transactions too.
- Payee names are the cleaned names shown in the app (e.g. "American Honda Finance"), not raw bank strings.

How to work:
- Look things up before proposing: use find_payees / find_transactions to see how a bill actually shows up and where it's filed now. Don't guess payee names.
- Propose changes with the change tools. Proposals are shown to the household with an Apply button; nothing changes until they apply. Propose the complete set needed, in the order it should happen (e.g. create a category before filing a payee into it). Refer to categories as "Group > Name" or by id.
- Be careful with history: if a payee is split across categories on purpose (a gas station that also sells groceries), don't move all of its history - set the rule only, or move specific transactions.
- If something is ambiguous and the answer changes what you'd do (which category a new bill belongs in, what the new amount is), ask one short question instead of guessing.
- Reply briefly in plain language: what you found, what you're proposing, and anything they should know (like a category showing as unbudgeted). No markdown tables or headers; short paragraphs or a few bullets. Amounts in dollars."""


def _money(cents):
    return ("-" if cents < 0 else "") + "${:,.2f}".format(abs(cents) / 100)


# ---------- context snapshot ----------

def _snapshot() -> str:
    today = date.today()
    cats = Category.query.order_by(Category.is_income.desc(), Category.group_name, Category.name).all()
    ids = [c.id for c in cats]
    budget, _ = _budgets_as_of(today.year, today.month, ids)
    first = date(today.year, today.month, 1)
    start3 = date(first.year - (first.month <= 3), (first.month - 4) % 12 + 1, 1)
    spent_month = dict(db.session.query(Transaction.category_id, func.sum(Transaction.amount))
                       .filter(Transaction.date >= first).group_by(Transaction.category_id))
    spent3 = dict(db.session.query(Transaction.category_id, func.sum(Transaction.amount))
                  .filter(Transaction.date >= start3, Transaction.date < first).group_by(Transaction.category_id))
    lines = [f"Today is {today.isoformat()}.", "",
             "## Categories (id | Group > Name | status | budget this month | spent this month | avg/month over the 3 previous months)"]
    for c in cats:
        status = "retired" if c.hidden else ("income" if c.is_income else "active")
        s, a = -(spent_month.get(c.id) or 0), -(spent3.get(c.id) or 0) / 3
        if c.is_income:
            s, a = -s, -a
        lines.append(f"{c.id} | {c.group_name} > {c.name} | {status} | {_money(budget.get(c.id, 0))} | {_money(s)} | {_money(round(a))}")

    year_ago = today - timedelta(days=365)
    names = {c.id: f"{c.group_name} > {c.name}" for c in cats}
    rules = {}
    for r in PayeeRule.query.all():
        rules.setdefault(r.payee, r.category_id)
    rows = (db.session.query(Transaction.payee, Transaction.category_id, func.count(), func.sum(Transaction.amount))
            .filter(Transaction.date >= year_ago, Transaction.payee.isnot(None))
            .group_by(Transaction.payee, Transaction.category_id).all())
    payees = defaultdict(lambda: {"n": 0, "total": 0, "cats": defaultdict(int)})
    for p, cid, n, total in rows:
        payees[p]["n"] += n
        payees[p]["total"] += total or 0
        payees[p]["cats"][cid] += n
    top = sorted(payees.items(), key=lambda kv: -kv[1]["n"])[:120]
    lines += ["", "## Most frequent payees, last 12 months (payee | count | total | where filed | rule)"]
    for p, d in top:
        filed = ", ".join(f"{n}× {names.get(cid, 'uncategorized')}" for cid, n in sorted(d["cats"].items(), key=lambda kv: -kv[1]))
        rule = names.get(rules[p], "?") if p in rules else "none"
        lines.append(f"{p} | {d['n']} | {_money(d['total'])} | {filed} | {rule}")
    lines.append(f"({len(payees)} payees in total; use find_payees for the rest.)")
    return "\n".join(lines)


# ---------- tools ----------

CAT_REF = {"type": "string", "description": 'Category as "Group > Name" (e.g. "Subscriptions > Claude") or its numeric id.'}

TOOLS = [
    {"name": "find_payees",
     "description": "Search payees by name (case-insensitive substring). Returns count, total, date range, where each is filed, and any rule.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_transactions",
     "description": "List transactions, newest first. Filter by payee substring, category, and/or date range. Amounts: negative = spending.",
     "input_schema": {"type": "object", "properties": {
         "payee_contains": {"type": "string"},
         "category": CAT_REF,
         "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
         "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
         "limit": {"type": "integer", "description": "Max rows, default 40, at most 150"}}}},
    {"name": "propose_set_budget",
     "description": "Propose a monthly budget for a category.",
     "input_schema": {"type": "object", "properties": {
         "category": CAT_REF,
         "amount": {"type": "number", "description": "Dollars per month (0 to stop budgeting it)"},
         "this_month_only": {"type": "boolean", "description": "Only this month; next month returns to the previous amount. Default false."}},
         "required": ["category", "amount"]}},
    {"name": "propose_create_category",
     "description": "Propose a new category. Follow with propose_set_budget to give it a budget.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "group": {"type": "string", "description": "Existing or new group name"},
         "is_income": {"type": "boolean"}}, "required": ["name", "group"]}},
    {"name": "propose_update_category",
     "description": "Propose renaming a category, moving it to another group, or retiring/restoring it.",
     "input_schema": {"type": "object", "properties": {
         "category": CAT_REF, "new_name": {"type": "string"}, "new_group": {"type": "string"},
         "retired": {"type": "boolean", "description": "true to retire, false to restore"}},
         "required": ["category"]}},
    {"name": "propose_file_payee",
     "description": "Propose filing a payee under a category: sets the rule for future imports, and with move_history also moves its past transactions.",
     "input_schema": {"type": "object", "properties": {
         "payee": {"type": "string", "description": "Exact payee name as shown in the app"},
         "category": CAT_REF,
         "move_history": {"type": "boolean", "description": "Also move past transactions. Default true."}},
         "required": ["payee", "category"]}},
    {"name": "propose_rename_payee",
     "description": "Propose renaming a payee everywhere (transactions and rules).",
     "input_schema": {"type": "object", "properties": {
         "payee": {"type": "string"}, "new_name": {"type": "string"}}, "required": ["payee", "new_name"]}},
    {"name": "propose_forget_payee_rule",
     "description": "Propose removing a payee's rule so new imports go to Review. Past transactions stay put.",
     "input_schema": {"type": "object", "properties": {"payee": {"type": "string"}}, "required": ["payee"]}},
    {"name": "propose_move_transactions",
     "description": "Propose moving specific transactions (by id, from find_transactions) to a category. For one-offs.",
     "input_schema": {"type": "object", "properties": {
         "transaction_ids": {"type": "array", "items": {"type": "integer"}}, "category": CAT_REF},
         "required": ["transaction_ids", "category"]}},
]


class ToolError(Exception):
    pass


def _resolve_cat(ref, pending=(), allow_retired=True):
    """Category for a "Group > Name" or id reference. A name created by an
    earlier proposal in `pending` resolves to a placeholder dict."""
    if ref is None or ref == "":
        raise ToolError("No category given.")
    s = str(ref).strip()
    cat = None
    if s.isdigit():
        cat = db.session.get(Category, int(s))
    elif ">" in s:
        g, n = (x.strip().lower() for x in s.split(">", 1))
        cat = Category.query.filter(func.lower(Category.group_name) == g, func.lower(Category.name) == n).first()
        if not cat:
            for p in pending:
                if p["tool"] == "propose_create_category" and (p["input"]["group"].strip().lower(), p["input"]["name"].strip().lower()) == (g, n):
                    return {"new": True, "label": f'{p["input"]["group"]} > {p["input"]["name"]}'}
    if not cat:
        raise ToolError(f'No category "{s}". Use "Group > Name" exactly as listed, or its id.')
    if cat.hidden and not allow_retired:
        raise ToolError(f"{cat.group_name} > {cat.name} is retired; restore it first (propose_update_category with retired=false) or pick another.")
    return cat


def _label(cat):
    return cat["label"] if isinstance(cat, dict) else f"{cat.group_name} > {cat.name}"


def _pending_restores(pending):
    out = set()
    for p in pending:
        if p["tool"] == "propose_update_category" and p["input"].get("retired") is False:
            ref = str(p["input"]["category"]).strip()
            try:
                c = _resolve_cat(ref)
                out.add(c.id)
            except ToolError:
                pass
    return out


def _find_payees(inp):
    q = (inp.get("query") or "").strip().lower()
    if not q:
        raise ToolError("Empty query.")
    names = {c.id: f"{c.group_name} > {c.name}" for c in Category.query}
    rows = (db.session.query(Transaction.payee, Transaction.category_id, func.count(), func.sum(Transaction.amount),
                             func.min(Transaction.date), func.max(Transaction.date))
            .filter(func.lower(Transaction.payee).contains(q))
            .group_by(Transaction.payee, Transaction.category_id).all())
    by = defaultdict(list)
    for p, cid, n, total, lo, hi in rows:
        by[p].append((cid, n, total or 0, lo, hi))
    rules = defaultdict(list)
    for r in PayeeRule.query.filter(func.lower(PayeeRule.payee).contains(q) | func.lower(PayeeRule.match).contains(q)):
        rules[r.payee].append(f'"{r.match}" → {names.get(r.category_id, "no category")}')
    if not by and not rules:
        return f'No payees match "{q}".'
    out = []
    for p in sorted(set(by) | set(rules), key=lambda p: -sum(x[1] for x in by.get(p, []))):
        parts = by.get(p, [])
        n = sum(x[1] for x in parts)
        line = f"{p}: {n} transactions"
        if parts:
            line += f", total {_money(sum(x[2] for x in parts))}, {min(x[3] for x in parts)} to {max(x[4] for x in parts)}; filed: " + \
                    ", ".join(f"{x[1]}× {names.get(x[0], 'uncategorized')}" for x in sorted(parts, key=lambda x: -x[1]))
        line += "; rules: " + ("; ".join(rules[p]) if rules.get(p) else "none")
        out.append(line)
    return "\n".join(out[:30])


def _find_transactions(inp):
    q = _categorizable()
    if inp.get("payee_contains"):
        q = q.filter(func.lower(Transaction.payee).contains(inp["payee_contains"].strip().lower()))
    if inp.get("category") not in (None, ""):
        q = q.filter(Transaction.category_id == _resolve_cat(inp["category"]).id)
    for key, op in (("start_date", "__ge__"), ("end_date", "__le__")):
        if inp.get(key):
            try:
                d = date.fromisoformat(inp[key])
            except ValueError:
                raise ToolError(f"{key} must be YYYY-MM-DD.")
            q = q.filter(getattr(Transaction.date, op)(d))
    limit = max(1, min(int(inp.get("limit") or 40), 150))
    names = {c.id: f"{c.group_name} > {c.name}" for c in Category.query}
    rows = q.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit).all()
    if not rows:
        return "No matching transactions."
    return "id | date | payee | amount | category | memo\n" + "\n".join(
        f"{t.id} | {t.date} | {t.payee} | {_money(t.amount)} | {names.get(t.category_id, 'uncategorized')} | {(t.memo or '')[:60]}"
        for t in rows)


def _payee_count(payee):
    return _categorizable().filter(Transaction.payee == payee).count()


def _validate(tool, inp, pending):
    """Check a proposal against the current data (plus earlier pending
    proposals) and return its plain-English summary. Raises ToolError."""
    if tool == "propose_set_budget":
        cat = _resolve_cat(inp.get("category"), pending)
        if not isinstance(cat, dict) and cat.hidden and cat.id not in _pending_restores(pending):
            raise ToolError(f"{_label(cat)} is retired; restore it first or pick another category.")
        try:
            amt = float(inp.get("amount"))
        except (TypeError, ValueError):
            raise ToolError("amount must be a number of dollars.")
        if amt < 0:
            raise ToolError("amount can't be negative.")
        when = "for this month only" if inp.get("this_month_only") else "a month from now on"
        cur = ""
        if not isinstance(cat, dict):
            t = date.today()
            cur = f" (now {_money(_budgets_as_of(t.year, t.month, [cat.id])[0].get(cat.id, 0))})"
        return f"Budget {_label(cat)} at {_money(round(amt * 100))} {when}{cur}"
    if tool == "propose_create_category":
        name, group = (inp.get("name") or "").strip(), (inp.get("group") or "").strip()
        if not name or not group:
            raise ToolError("A category needs a name and a group.")
        if Category.query.filter(func.lower(Category.name) == name.lower(), func.lower(Category.group_name) == group.lower()).first():
            raise ToolError(f"{group} > {name} already exists (it may be retired; restore it with propose_update_category instead).")
        return f"Add category {group} > {name}" + (" (income)" if inp.get("is_income") else "")
    if tool == "propose_update_category":
        cat = _resolve_cat(inp.get("category"), pending)
        if isinstance(cat, dict):
            raise ToolError("Set the new category's details in propose_create_category instead.")
        bits = []
        if inp.get("new_name") or inp.get("new_group"):
            bits.append(f'rename to {(inp.get("new_group") or cat.group_name).strip()} > {(inp.get("new_name") or cat.name).strip()}')
        if inp.get("retired") is True and not cat.hidden:
            bits.append("retire it (history kept)")
        if inp.get("retired") is False and cat.hidden:
            bits.append("restore it")
        if not bits:
            raise ToolError("Nothing to change.")
        return f"{_label(cat)}: " + ", ".join(bits)
    if tool == "propose_file_payee":
        payee = (inp.get("payee") or "").strip()
        n = _payee_count(payee)
        if not n and not PayeeRule.query.filter_by(payee=payee).first():
            raise ToolError(f'No payee named exactly "{payee}". Use find_payees to get the exact name.')
        cat = _resolve_cat(inp.get("category"), pending)
        if not isinstance(cat, dict) and cat.hidden and cat.id not in _pending_restores(pending):
            raise ToolError(f"{_label(cat)} is retired; restore it first or pick another category.")
        if inp.get("move_history", True) and n:
            moving = _categorizable().filter(Transaction.payee == payee)
            if not isinstance(cat, dict):
                moving = moving.filter((Transaction.category_id != cat.id) | Transaction.category_id.is_(None))
            m = moving.count()
            return (f"File {payee} under {_label(cat)}: move {m} past transaction{'s' if m != 1 else ''} and future imports"
                    if m else f"File future {payee} imports under {_label(cat)} (past ones are already there)")
        return f"File future {payee} imports under {_label(cat)} (past transactions stay where they are)"
    if tool == "propose_rename_payee":
        payee, new = (inp.get("payee") or "").strip(), (inp.get("new_name") or "").strip()
        n = _payee_count(payee)
        if not n:
            raise ToolError(f'No payee named exactly "{payee}".')
        if not new or new == payee:
            raise ToolError("Give a different new name.")
        return f"Rename {payee} to {new} ({n} transaction{'s' if n != 1 else ''})"
    if tool == "propose_forget_payee_rule":
        payee = (inp.get("payee") or "").strip()
        if not PayeeRule.query.filter_by(payee=payee).first():
            raise ToolError(f'{payee} has no rule.')
        return f"Stop filing {payee} automatically (new imports go to Review)"
    if tool == "propose_move_transactions":
        ids = [int(i) for i in (inp.get("transaction_ids") or []) if str(i).isdigit()]
        if not ids:
            raise ToolError("No transaction ids.")
        found = _categorizable().filter(Transaction.id.in_(ids)).all()
        missing = set(ids) - {t.id for t in found}
        if missing:
            raise ToolError(f"Unknown or split/transfer transaction ids: {sorted(missing)}")
        cat = _resolve_cat(inp.get("category"), pending)
        if not isinstance(cat, dict) and cat.hidden and cat.id not in _pending_restores(pending):
            raise ToolError(f"{_label(cat)} is retired; restore it first or pick another category.")
        desc = ", ".join(f"{t.payee} {_money(t.amount)} on {t.date:%b %-d}" for t in found[:4])
        more = f" and {len(found) - 4} more" if len(found) > 4 else ""
        return f"Move {desc}{more} to {_label(cat)}"
    raise ToolError(f"Unknown tool {tool}.")


def _apply(p):
    """Apply one stored proposal. Raises ValueError/ToolError on failure."""
    tool, inp = p["tool"], p["input"]
    if tool == "propose_create_category":
        save_category(None, inp["name"], inp["group"], hidden=False, is_income=bool(inp.get("is_income")))
    elif tool == "propose_update_category":
        cat = _resolve_cat(inp["category"])
        save_category(cat, inp.get("new_name") or cat.name, inp.get("new_group") or cat.group_name,
                      hidden=inp.get("retired"))
    elif tool == "propose_set_budget":
        set_budget(_resolve_cat(inp["category"], allow_retired=False), round(float(inp["amount"]) * 100),
                   bool(inp.get("this_month_only")))
    elif tool == "propose_file_payee":
        file_payee(inp["payee"].strip(), _resolve_cat(inp["category"], allow_retired=False),
                   history=inp.get("move_history", True))
    elif tool == "propose_rename_payee":
        file_payee(inp["payee"].strip(), None, new_payee=inp["new_name"])
    elif tool == "propose_forget_payee_rule":
        PayeeRule.query.filter(PayeeRule.payee == inp["payee"].strip()).delete(synchronize_session=False)
    elif tool == "propose_move_transactions":
        cat = _resolve_cat(inp["category"], allow_retired=False)
        for t in _categorizable().filter(Transaction.id.in_(inp["transaction_ids"])):
            t.category_id = cat.id
    db.session.flush()


# ---------- Claude ----------

_client = None


def _claude():
    global _client
    if _client is None:
        key = os.environ.get("CLAUDE_API_KEY")
        if not key:
            raise RuntimeError("CLAUDE_API_KEY isn't set on the server.")
        _client = anthropic.Anthropic(api_key=key, timeout=55.0, max_retries=1)
    return _client


def _load(chat):
    return json.loads(chat.messages), json.loads(chat.transcript), json.loads(chat.proposals)


def _save(chat, messages, transcript, proposals):
    chat.messages, chat.transcript, chat.proposals = (json.dumps(messages), json.dumps(transcript),
                                                     json.dumps(proposals))


def _view(chat, status=None):
    _, transcript, proposals = _load(chat)
    return {"chat_id": chat.id, "done": chat.done, "status": status,
            "transcript": transcript, "proposals": proposals}


def _step(chat):
    """One Claude call; runs lookups and records proposals from its tool calls."""
    messages, transcript, proposals = _load(chat)
    try:
        resp = _claude().beta.messages.create(
            model=MODEL, max_tokens=16000,
            system=SYSTEM, tools=TOOLS, messages=messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.RateLimitError:
        return "Claude is busy right now (rate limited). Try again in a minute."
    except anthropic.APIStatusError as e:
        return f"Claude returned an error ({e.status_code}). Try again."
    except anthropic.APIConnectionError:
        return "Couldn't reach Claude. Try again."

    messages.append({"role": "assistant", "content": [b.to_dict() for b in resp.content]})
    chat.steps += 1
    text = "\n\n".join(b.text for b in resp.content if b.type == "text" and b.text.strip())

    if resp.stop_reason == "tool_use":
        results, status = [], "Thinking…"
        pending = [p for p in proposals if p["status"] == "pending"]
        for b in resp.content:
            if b.type != "tool_use":
                continue
            inp = b.input if isinstance(b.input, dict) else {}
            try:
                if b.name == "find_payees":
                    out, status = _find_payees(inp), f'Looking up payees matching "{inp.get("query", "")}"…'
                elif b.name == "find_transactions":
                    out, status = _find_transactions(inp), "Looking through transactions…"
                else:
                    summary = _validate(b.name, inp, pending)
                    p = {"id": len(proposals) + 1, "tool": b.name, "input": inp, "summary": summary, "status": "pending"}
                    proposals.append(p)
                    pending.append(p)
                    out, status = f"Proposal #{p['id']} recorded (not applied yet): {summary}", "Drafting changes…"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            except (ToolError, ValueError) as e:
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(e), "is_error": True})
        messages.append({"role": "user", "content": results})
        if chat.steps >= MAX_STEPS:
            chat.done = True
            transcript.append({"role": "assistant", "text": (text + "\n\n" if text else "") +
                               "I ran out of steps before finishing. Tell me what's left and I'll keep going."})
        _save(chat, messages, transcript, proposals)
        return status

    chat.done = True
    if resp.stop_reason == "refusal":
        text = "I can't help with that one."
    elif resp.stop_reason == "max_tokens":
        text = (text + "\n\n" if text else "") + "(My reply got cut off. Ask me to continue.)"
    transcript.append({"role": "assistant", "text": text or "Done."})
    _save(chat, messages, transcript, proposals)
    return None


# ---------- routes ----------

def _chat_or_404(cid):
    chat = db.session.get(AssistantChat, cid) if cid else None
    if not chat:
        abort(404)
    return chat


@v2_bp.route("/manage/assistant/<int:cid>")
@login_required
def assistant_get(cid):
    return jsonify(_view(_chat_or_404(cid)))


@v2_bp.route("/manage/assistant/step", methods=["POST"])
@login_required
def assistant_step():
    """Body: {"chat_id": int?, "message": str?}. With a message, starts a turn
    (and a chat if there's none); without one, continues the current turn."""
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()[:4000]
    chat = db.session.get(AssistantChat, data["chat_id"]) if data.get("chat_id") else None

    if text:
        if chat and not chat.done:
            return jsonify({"error": "Still working on the last message."}), 409
        if not chat:
            chat = AssistantChat()
            db.session.add(chat)
            db.session.flush()
        messages, transcript, proposals = _load(chat)
        note = []
        dropped = [p for p in proposals if p["status"] == "pending"]
        for p in dropped:
            p["status"] = "dropped"
        if dropped:
            note.append("(None of your earlier pending proposals were applied. Propose the full set again if they're still wanted.)")
        if not messages:
            content = f"<budget_snapshot>\n{_snapshot()}\n</budget_snapshot>\n\n{text}"
        else:
            content = "\n".join(note + [text])
        messages.append({"role": "user", "content": content})
        transcript.append({"role": "user", "text": text})
        chat.steps, chat.done = 0, False
        _save(chat, messages, transcript, proposals)
        db.session.commit()
        return jsonify(_view(chat, "Reading your budget…"))

    if not chat:
        return jsonify({"error": "No conversation to continue."}), 400
    if chat.done:
        return jsonify(_view(chat))
    status = _step(chat)
    db.session.commit()
    return jsonify(_view(chat, status))


def _backup() -> str:
    """Copy the SQLite database next to itself before applying changes, and
    prune old assistant backups."""
    path = db.engine.url.database
    if not path:
        return ""
    dest = f"{path}.assistant-{datetime.now():%Y%m%d%H%M%S}"
    raw = db.engine.raw_connection()
    try:
        with sqlite3.connect(dest) as out:
            raw.driver_connection.backup(out)
    finally:
        raw.close()
    folder, base = os.path.split(path)
    old = sorted(f for f in os.listdir(folder or ".") if f.startswith(base + ".assistant-"))
    for f in old[:-KEEP_BACKUPS]:
        os.remove(os.path.join(folder, f))
    return dest


@v2_bp.route("/manage/assistant/apply", methods=["POST"])
@login_required
def assistant_apply():
    """Body: {"chat_id": int, "ids": [proposal ids]}. Applies the selected
    pending proposals in order, all or nothing."""
    data = request.get_json(silent=True) or {}
    chat = _chat_or_404(data.get("chat_id"))
    if not chat.done:
        return jsonify({"error": "Claude is still working."}), 409
    messages, transcript, proposals = _load(chat)
    want = {int(i) for i in data.get("ids") or []}
    todo = [p for p in proposals if p["id"] in want and p["status"] == "pending"]
    if not todo:
        return jsonify({"error": "Nothing selected to apply."}), 400
    _backup()
    try:
        for p in todo:
            _apply(p)
    except (ToolError, ValueError, KeyError, TypeError) as e:
        db.session.rollback()
        return jsonify({"error": f"Nothing was changed: {p['summary']} failed ({e})."}), 400
    for p in proposals:
        if p["status"] == "pending":
            p["status"] = "applied" if p["id"] in want else "skipped"
    applied = [p for p in todo]
    note = "Applied: " + "; ".join(f"#{p['id']} {p['summary']}" for p in applied)
    left = [p for p in proposals if p["status"] == "skipped" and p["id"] not in want]
    if left:
        note += ". Not applied: " + "; ".join(f"#{p['id']} {p['summary']}" for p in left)
    # Recorded for Claude's context on the next message; shown in the transcript too.
    messages.append({"role": "user", "content": f"({note})"})
    messages.append({"role": "assistant", "content": [{"type": "text", "text": "Noted."}]})
    transcript.append({"role": "note", "text": f"Applied {len(applied)} change{'s' if len(applied) != 1 else ''}."})
    _save(chat, messages, transcript, proposals)
    db.session.commit()
    return jsonify({"ok": True, **_view(chat)})
