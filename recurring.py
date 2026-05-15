"""
Recurring transaction processor.

When the app loads any page, we call `post_due_recurring(db)` which:
  1. Finds all active recurring transactions where next_date <= today.
  2. Creates a real Transaction for each.
  3. Advances next_date by the appropriate step.

This means the user always sees an up-to-date ledger without needing a cron job.
"""
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from models import db, Recurring, Transaction


def _advance(d: date, frequency: str, interval: int) -> date:
    if frequency == "daily":
        return d + timedelta(days=interval)
    if frequency == "weekly":
        return d + timedelta(weeks=interval)
    if frequency == "biweekly":
        return d + timedelta(weeks=2 * interval)
    if frequency == "monthly":
        return d + relativedelta(months=interval)
    if frequency == "yearly":
        return d + relativedelta(years=interval)
    # Fallback: monthly
    return d + relativedelta(months=interval)


def post_due_recurring(today: date | None = None) -> int:
    """Materialize any recurring transactions whose next_date has arrived.
    Returns count of transactions created. Catches up if missed."""
    today = today or date.today()
    created = 0
    due = Recurring.query.filter(Recurring.active.is_(True),
                                  Recurring.next_date <= today).all()
    for r in due:
        # Catch up: if we missed multiple cycles (e.g. user didn't open the app
        # for a few months), post each one in turn.
        cursor = r.next_date
        while cursor <= today:
            t = Transaction(
                account_id=r.account_id,
                category_id=r.category_id,
                date=cursor,
                amount=r.amount,
                payee=r.payee,
                memo=r.memo or "(recurring)",
                cleared=False,
                source="recurring",
                recurring_id=r.id,
            )
            db.session.add(t)
            created += 1
            r.last_posted = cursor
            cursor = _advance(cursor, r.frequency, r.interval)
        r.next_date = cursor
    if created:
        db.session.commit()
    return created
