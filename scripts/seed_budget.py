"""
One-shot script: replace default categories with Andy & Libby's real budget
and set May 2026 envelope amounts.

Run with:
  cd /var/www/budget
  sudo -u www-data DATABASE_URL=sqlite:////var/lib/budget/budget.db \
      SECRET_KEY=placeholder FLASK_ENV=production \
      .venv/bin/python scripts/seed_budget.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from models import db, Category, BudgetMonth

app = create_app()

# ---------------------------------------------------------------------------
# Category definitions
# (group, name, is_income, sort_order)
# ---------------------------------------------------------------------------
CATEGORIES = [
    # Income
    ("Income", "Paycheck - Andy",       True,  1),
    ("Income", "Paycheck - Libby",      True,  2),
    ("Income", "Other Income",          True,  3),

    # Housing
    ("Housing", "Fish Lake",            False, 1),
    ("Housing", "Cabin - Siren",        False, 2),

    # Cabin Expenses
    ("Cabin", "Snow Removal & Lawn",    False, 1),
    ("Cabin", "City Utilities",         False, 2),
    ("Cabin", "Northern Electric",      False, 3),
    ("Cabin", "Pest Control",           False, 4),
    ("Cabin", "Sirentel (Internet)",    False, 5),

    # Car Payments
    ("Car Payments", "Honda CRV",       False, 1),
    ("Car Payments", "Van Loan",        False, 2),
    ("Car Payments", "Subaru",          False, 3),

    # Insurance
    ("Insurance", "Progressive - Andy", False, 1),
    ("Insurance", "Progressive - Libby",False, 2),
    ("Insurance", "Van Insurance",      False, 3),
    ("Insurance", "Boat Insurance",     False, 4),

    # Debt
    ("Debt", "CC Debt",                 False, 1),
    ("Debt", "CC Loan",                 False, 2),
    ("Debt", "Student Loan",            False, 3),
    ("Debt", "CPAP Loan",               False, 4),

    # Food & Dining
    ("Food & Dining", "Groceries",      False, 1),
    ("Food & Dining", "Food & Drink",   False, 2),
    ("Food & Dining", "Dining Out",     False, 3),

    # Transportation
    ("Transportation", "Gas",           False, 1),

    # Health
    ("Health", "Therapy",               False, 1),

    # Phone
    ("Phone", "Phone - Andy",           False, 1),
    ("Phone", "Phone - Libby",          False, 2),

    # Pets
    ("Pets", "Dog Food",                False, 1),

    # Personal
    ("Personal", "Something Fun & Gifts", False, 1),
    ("Personal", "Toiletry",            False, 2),
    ("Personal", "Hobbies",             False, 3),

    # Subscriptions
    ("Subscriptions", "Netflix",        False, 1),
    ("Subscriptions", "Discovery Plus", False, 2),

    # Savings
    ("Savings", "Savings",              False, 1),
]

# ---------------------------------------------------------------------------
# Monthly budget amounts in DOLLARS (converted to cents below)
# Van Loan: $412 is the household payment (appears in both Andy's & Libby's
# personal sheets because they split the responsibility, but it's one payment).
# Gas: Andy $120 + Libby $90 = $210 combined.
# Dog Food: Andy $20 + Libby $80 = $100 combined.
# ---------------------------------------------------------------------------
AMOUNTS = {
    # Housing
    "Fish Lake":              890,
    "Cabin - Siren":          574,
    # Cabin
    "Snow Removal & Lawn":    130,
    "City Utilities":         100,
    "Northern Electric":       75,
    "Pest Control":            65,
    "Sirentel (Internet)":     34,
    # Car Payments
    "Honda CRV":              590,
    "Van Loan":               412,
    "Subaru":                 458,
    # Insurance
    "Progressive - Andy":     120,
    "Progressive - Libby":     92,
    "Van Insurance":          150,
    "Boat Insurance":          15,
    # Debt
    "CC Debt":                300,
    "CC Loan":                228,
    "Student Loan":            90,
    "CPAP Loan":              100,
    # Food & Dining
    "Groceries":              250,
    "Food & Drink":           700,
    "Dining Out":             200,
    # Transportation
    "Gas":                    210,
    # Health
    "Therapy":                200,
    # Phone
    "Phone - Andy":            20,
    "Phone - Libby":          110,
    # Pets
    "Dog Food":               100,
    # Personal
    "Something Fun & Gifts":  250,
    "Toiletry":                62,
    "Hobbies":                  0,
    # Subscriptions
    "Netflix":                 20,
    "Discovery Plus":          15,
    # Savings
    "Savings":                  0,
}

YEAR, MONTH = 2026, 5

with app.app_context():
    # Wipe existing categories (no transactions yet, so no FK risk)
    BudgetMonth.query.delete()
    Category.query.delete()
    db.session.commit()

    # Insert new categories
    cat_map = {}
    for group, name, is_income, sort_order in CATEGORIES:
        c = Category(group_name=group, name=name, is_income=is_income, sort_order=sort_order)
        db.session.add(c)
        db.session.flush()  # get the id
        cat_map[name] = c

    db.session.commit()

    # Set May 2026 envelope amounts
    for name, dollars in AMOUNTS.items():
        c = cat_map[name]
        bm = BudgetMonth(
            year=YEAR,
            month=MONTH,
            category_id=c.id,
            assigned=dollars * 100,  # store as cents
        )
        db.session.add(bm)

    db.session.commit()

    print(f"Done. Created {len(CATEGORIES)} categories and {len(AMOUNTS)} budget envelopes for {YEAR}-{MONTH:02d}.")
    print()

    # Summary
    total = sum(AMOUNTS.values())
    print(f"Total monthly budget: ${total:,.2f}")
    from itertools import groupby
    rows = sorted([(g, n, AMOUNTS.get(n, 0)) for g, n, _, _ in CATEGORIES if not [i for _, ni, ii, _ in CATEGORIES if ni == n and ii]], key=lambda x: x[0])
    current_group = None
    group_total = 0
    for group, name, amt in rows:
        if group != current_group:
            if current_group:
                print(f"  Subtotal: ${group_total:,.2f}")
            print(f"\n{group}")
            current_group = group
            group_total = 0
        print(f"  {name:<30} ${amt:>7,.2f}")
        group_total += amt
    if current_group:
        print(f"  Subtotal: ${group_total:,.2f}")
