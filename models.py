"""
SQLAlchemy models for the local budget app.

Design notes:
- All amounts are stored as integer CENTS to avoid floating-point drift.
  Helpers `to_cents()` / `from_cents()` convert at the boundary.
- Transaction.amount is signed: negative = outflow, positive = inflow.
- A "transfer" is two transactions linked by transfer_id with opposite signs.
- BudgetMonth holds the envelope assignment per category per month.
- "Available" for a category in a month = carryover + assigned - spent.
"""
from datetime import date, datetime
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, Index
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


# Hard cap on the number of users allowed to exist. We treat this household
# budget as a two-person system; the auth code refuses to create more.
MAX_USERS = 2


class User(UserMixin, db.Model):
    """A login for the household. All budget data is shared across users."""
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(120), nullable=False, default="")
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)

    def set_password(self, plaintext: str) -> None:
        # scrypt (werkzeug 2.3+ default) — strong, slow-by-design, no extra deps.
        self.password_hash = generate_password_hash(plaintext)

    def check_password(self, plaintext: str) -> bool:
        return check_password_hash(self.password_hash, plaintext)


# ---------- money helpers ----------

def to_cents(value) -> int:
    """Parse a user-entered amount (str/float/Decimal) into integer cents."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip().replace(",", "").replace("$", "")
    if s == "" or s == "-":
        return 0
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    f = float(s)
    cents = int(round(f * 100))
    return -cents if neg else cents


def from_cents(cents: int) -> float:
    """Convert integer cents to a float dollar value for display."""
    return (cents or 0) / 100.0


def fmt_money(cents: int) -> str:
    """Format integer cents as a human-readable money string."""
    cents = cents or 0
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


# ---------- models ----------

class Account(db.Model):
    __tablename__ = "accounts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    type = db.Column(db.String(32), nullable=False, default="checking")
    # On-budget accounts (checking, savings, cash, credit) participate in budgeting.
    # Off-budget accounts (investment, loan) just track balances.
    on_budget = db.Column(db.Boolean, nullable=False, default=True)
    starting_balance = db.Column(db.Integer, nullable=False, default=0)  # cents
    closed = db.Column(db.Boolean, nullable=False, default=False)
    # Optional Plaid linkage:
    plaid_account_id = db.Column(db.String(64))
    plaid_item_id = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship("Transaction", back_populates="account",
                                   cascade="all, delete-orphan")

    @property
    def balance(self) -> int:
        """Live balance = starting balance + sum of all transactions on this account."""
        total = self.starting_balance or 0
        for t in self.transactions:
            total += t.amount
        return total


class Category(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    group_name = db.Column(db.String(120), nullable=False, default="General")
    is_income = db.Column(db.Boolean, nullable=False, default=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, default=0)

    __table_args__ = (UniqueConstraint("name", "group_name", name="uq_cat_name_group"),)


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    date = db.Column(db.Date, nullable=False, default=date.today)
    amount = db.Column(db.Integer, nullable=False)  # cents, signed
    payee = db.Column(db.String(200), default="")
    memo = db.Column(db.String(400), default="")
    cleared = db.Column(db.Boolean, nullable=False, default=False)

    # For transfers: links the two halves together.
    transfer_id = db.Column(db.String(36))

    # Provenance:
    source = db.Column(db.String(32), default="manual")  # manual | csv | plaid | recurring
    external_id = db.Column(db.String(120))  # plaid txn id or csv hash, for dedupe
    recurring_id = db.Column(db.Integer, db.ForeignKey("recurring.id"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship("Account", back_populates="transactions")
    category = db.relationship("Category")

    __table_args__ = (
        Index("ix_txn_date", "date"),
        Index("ix_txn_account_date", "account_id", "date"),
        UniqueConstraint("external_id", name="uq_txn_external_id"),
    )


class BudgetMonth(db.Model):
    """Envelope assignment for a single category in a single month."""
    __tablename__ = "budget_months"
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1..12
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    assigned = db.Column(db.Integer, nullable=False, default=0)  # cents
    note = db.Column(db.String(400), default="")

    __table_args__ = (UniqueConstraint("year", "month", "category_id",
                                       name="uq_budget_ym_cat"),)


class Goal(db.Model):
    __tablename__ = "goals"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    target_amount = db.Column(db.Integer, nullable=False, default=0)  # cents
    target_date = db.Column(db.Date)
    # For "save up" goals: track contributions over time. We store the
    # running saved total and let the user log contributions.
    saved_amount = db.Column(db.Integer, nullable=False, default=0)
    note = db.Column(db.String(400), default="")
    completed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship("Category")

    @property
    def percent_complete(self) -> float:
        if not self.target_amount:
            return 0.0
        return max(0.0, min(100.0, 100.0 * self.saved_amount / self.target_amount))


class Recurring(db.Model):
    """A scheduled, repeating transaction."""
    __tablename__ = "recurring"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    amount = db.Column(db.Integer, nullable=False)  # cents, signed
    payee = db.Column(db.String(200), default="")
    memo = db.Column(db.String(400), default="")
    # Frequency: daily | weekly | biweekly | monthly | yearly
    frequency = db.Column(db.String(16), nullable=False, default="monthly")
    interval = db.Column(db.Integer, nullable=False, default=1)  # every N units
    next_date = db.Column(db.Date, nullable=False)
    last_posted = db.Column(db.Date)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship("Account")
    category = db.relationship("Category")


class PlaidItem(db.Model):
    """Stores a single linked Plaid Item (one institution login)."""
    __tablename__ = "plaid_items"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(64), unique=True, nullable=False)
    access_token = db.Column(db.String(200), nullable=False)
    institution_name = db.Column(db.String(120))
    cursor = db.Column(db.String(200))  # transactions/sync cursor
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
