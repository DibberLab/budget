"""
Bank sync: pull new, changed and removed transactions from Plaid.

Used by the Import page's "Sync" button and by the scheduled job
(scripts/sync_cron.py, every few hours via a systemd timer), so transactions
show up within hours of the bank posting them instead of whenever someone
remembers to press Sync.

- New transactions are imported, pending ones included (cleared=False), and
  saved payee rules file them on the way in.
- When a pending charge posts, Plaid sends the posted version with
  `pending_transaction_id`; the existing row is updated in place (new id,
  final amount/date, cleared) so its category and payee name carry over and
  it isn't counted twice.
- Bank corrections ("modified") update amount, date and cleared; the payee
  name and category the household chose are left alone.
- Removed transactions (a pending charge that dropped, a reversal) are
  deleted. Split transactions are never touched automatically; they're
  reported instead.

A file lock keeps two syncs (the button and the timer) from running at once
and importing the same transaction twice.
"""
import fcntl
import os
from contextlib import contextmanager

import plaid_client
from models import Account, PlaidItem, Transaction, db

LOCK_PATH = os.environ.get("BUDGET_SYNC_LOCK", "/var/lib/budget/sync.lock")


@contextmanager
def _sync_lock():
    path = LOCK_PATH if os.path.isdir(os.path.dirname(LOCK_PATH)) else "/tmp/budget-sync.lock"
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _cents(plaid_amount) -> int:
    # Plaid: positive = money out. App: negative = spending.
    return -int(round(float(plaid_amount) * 100))


def _set_bank_date(row: Transaction, bank_date) -> None:
    """Take the bank's date, unless someone re-dated this transaction by hand:
    then only remember what the bank says."""
    if row.date_manual:
        row.bank_date = bank_date
    else:
        row.date = bank_date


def run_sync() -> dict:
    """Sync every linked bank. Returns counts and any per-bank errors."""
    from v2 import AutoCat, _add_transaction, _rules_by_match  # late import: v2 imports this module's callers

    out = {"added": 0, "pending": 0, "posted": 0, "by_rule": 0, "modified": 0, "removed": 0,
           "skipped": [], "errors": [], "busy": False}
    with _sync_lock() as got:
        if not got:
            out["busy"] = True
            return out
        rules = _rules_by_match()
        by_ext = {t.external_id: t for t in Transaction.query.filter(Transaction.external_id.isnot(None))}
        for item in PlaidItem.query.all():
            bank = item.institution_name or "Bank"
            try:
                result = plaid_client.sync_transactions(item.access_token, item.cursor)
            except Exception as exc:
                out["errors"].append(f"{bank}: {exc}")
                continue
            accounts = {a.plaid_account_id: a.id for a in Account.query.filter(Account.plaid_item_id == item.item_id)}

            # Added first: a posted transaction re-keys its pending row before
            # the "removed" notice for the pending id is processed below.
            for t in result["added"]:
                acct, tid = accounts.get(t["account_id"]), t["transaction_id"]
                if not acct or tid in by_ext:
                    continue
                pending = bool(t.get("pending"))
                prior = by_ext.get(t.get("pending_transaction_id")) if not pending else None
                if prior is not None:
                    prior.external_id, prior.amount, prior.cleared = tid, _cents(t["amount"]), True
                    _set_bank_date(prior, t["date"])
                    by_ext.pop(t.get("pending_transaction_id"), None)
                    by_ext[tid] = prior
                    out["posted"] += 1
                    continue
                row, ruled = _add_transaction(
                    rules, account_id=acct, date=t["date"], amount=_cents(t["amount"]),
                    payee=t.get("name") or t.get("merchant_name") or "(bank)", memo="",
                    source="plaid", external_id=tid, cleared=not pending)
                by_ext[tid] = row
                out["added"] += 1
                out["pending"] += pending
                out["by_rule"] += ruled

            for t in result["modified"]:
                row = by_ext.get(t["transaction_id"])
                if row is None:
                    continue
                if row.splits:
                    out["skipped"].append(f"{row.payee} {row.date}: split, bank changed it")
                    continue
                row.amount, row.cleared = _cents(t["amount"]), not bool(t.get("pending"))
                _set_bank_date(row, t["date"])
                out["modified"] += 1

            for r in result["removed"]:
                row = by_ext.get(r["transaction_id"])
                if row is None:
                    continue
                if row.splits or row.transfer_id:
                    out["skipped"].append(f"{row.payee} {row.date}: bank removed it, left in place (split/transfer)")
                    continue
                AutoCat.query.filter_by(transaction_id=row.id).delete()
                db.session.delete(row)
                by_ext.pop(r["transaction_id"], None)
                out["removed"] += 1

            item.cursor = result["next_cursor"]
            db.session.commit()
    return out


def run_ai_filing(max_batches: int = 20) -> int:
    """File uncategorized transactions the AI is sure about (same as the
    Import page's auto-fill); unsure ones stay for Review. Returns how many
    transactions were filed."""
    from v2 import AI_BATCH, AutoCat, _ai_suggest, _uncategorized_groups

    filed, skip = 0, set()
    for _ in range(max_batches):
        pending = _uncategorized_groups(skip)
        batch = dict(list(pending.items())[:AI_BATCH])
        if not batch:
            break
        for sug in _ai_suggest(batch):
            if not sug["sure"]:
                continue
            for t in batch[sug["key"]]["txns"]:
                db.session.add(AutoCat(transaction_id=t.id, raw_payee=t.payee))
                if sug["payee"]:
                    t.payee = sug["payee"]
                t.category_id = sug["category_id"]
                filed += 1
        db.session.commit()
        skip.update(batch)
    return filed
