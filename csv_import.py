"""
CSV import for bank/credit-card statements.

Most banks let you export transactions as CSV. Columns vary wildly, so we
auto-detect common header names. The user can also remap columns manually
in the UI before importing.

Supported header aliases (case-insensitive):
    date:        date, transaction date, posted date, post date
    amount:      amount, transaction amount
    debit:       debit, withdrawal, money out
    credit:      credit, deposit, money in
    payee:       description, payee, merchant, name
    memo:        memo, notes, category (bank category, not budget category)
"""
import csv
import hashlib
import io
from datetime import datetime
from typing import Iterable

from dateutil import parser as dateparser

DATE_KEYS = ("date", "transaction date", "posted date", "post date", "trans date")
AMOUNT_KEYS = ("amount", "transaction amount", "amt")
DEBIT_KEYS = ("debit", "withdrawal", "money out", "withdrawals", "outflow")
CREDIT_KEYS = ("credit", "deposit", "money in", "deposits", "inflow")
PAYEE_KEYS = ("description", "payee", "merchant", "name", "details")
MEMO_KEYS = ("memo", "notes", "note")


def _pick(headers, candidates):
    """Return the first header (lowercased) matching any candidate, or None."""
    lower = {h.lower().strip(): h for h in headers if h}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def parse_csv(file_storage_or_text) -> tuple[list[dict], list[str]]:
    """
    Returns (rows, headers).
    Each row is a dict with normalized keys: date, amount (cents, signed),
    payee, memo, hash (for de-dup).
    """
    if hasattr(file_storage_or_text, "read"):
        raw = file_storage_or_text.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
    else:
        raw = file_storage_or_text

    # Sniff dialect; fall back to comma.
    try:
        dialect = csv.Sniffer().sniff(raw[:4096])
    except Exception:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    headers = reader.fieldnames or []

    date_col = _pick(headers, DATE_KEYS)
    amount_col = _pick(headers, AMOUNT_KEYS)
    debit_col = _pick(headers, DEBIT_KEYS)
    credit_col = _pick(headers, CREDIT_KEYS)
    payee_col = _pick(headers, PAYEE_KEYS)
    memo_col = _pick(headers, MEMO_KEYS)

    out = []
    for row in reader:
        if not row:
            continue
        try:
            d_raw = row.get(date_col) if date_col else None
            if not d_raw:
                continue
            d = dateparser.parse(d_raw, dayfirst=False).date()
        except Exception:
            continue

        # Compute signed cents from either a single amount column or split debit/credit.
        cents = 0
        if amount_col:
            cents = _to_cents(row.get(amount_col, ""))
        else:
            debit = _to_cents(row.get(debit_col, "")) if debit_col else 0
            credit = _to_cents(row.get(credit_col, "")) if credit_col else 0
            # Bank CSVs usually list both as positive numbers; debit is an outflow.
            cents = (abs(credit) if credit else 0) - (abs(debit) if debit else 0)

        payee = (row.get(payee_col, "") if payee_col else "").strip()
        memo = (row.get(memo_col, "") if memo_col else "").strip()

        # Hash for de-duplication on re-import. Include date + amount + payee so
        # legitimately repeated charges (Netflix every month) still come through
        # on different dates.
        h = hashlib.sha1(
            f"{d.isoformat()}|{cents}|{payee.lower()}".encode("utf-8")
        ).hexdigest()

        out.append({
            "date": d,
            "amount": cents,
            "payee": payee,
            "memo": memo,
            "hash": h,
        })
    return out, headers


def _to_cents(value) -> int:
    if value is None:
        return 0
    s = str(value).strip().replace(",", "").replace("$", "")
    if not s:
        return 0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    if s.startswith("-"):
        neg = True
        s = s[1:]
    try:
        f = float(s)
    except ValueError:
        return 0
    cents = int(round(f * 100))
    return -cents if neg else cents
