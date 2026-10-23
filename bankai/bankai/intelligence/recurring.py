"""Recurring-transaction detection: finds bills/subscriptions/paychecks and predicts
the next occurrence. Pure logic over (date, amount, merchant) tuples."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Transaction

_DIGITS = re.compile(r"\d+")

# cadence name -> (min days, max days, nominal days)
_CADENCES: list[tuple[str, int, int, int]] = [
    ("weekly", 5, 9, 7),
    ("biweekly", 12, 17, 14),
    ("monthly", 26, 35, 30),
    ("quarterly", 80, 100, 91),
    ("yearly", 340, 390, 365),
]


@dataclass
class RecurringSeries:
    merchant: str
    cadence: str
    avg_amount: float
    last_date: date
    next_date: date
    count: int
    account_id: str
    is_bill: bool  # outflow


def merchant_key(normalized_desc: str) -> str:
    return _DIGITS.sub("", normalized_desc).strip()[:80]


def _classify_intervals(dates: list[date]) -> tuple[str, int] | None:
    if len(dates) < 3:
        return None
    intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
    med = median(intervals)
    for name, lo, hi, nominal in _CADENCES:
        if lo <= med <= hi:
            tolerance = max(4, nominal // 5)
            if all(abs(iv - med) <= tolerance for iv in intervals):
                return name, nominal
    return None


def detect_recurring_from_rows(
    rows: list[tuple[str, str, date, float]],
) -> list[RecurringSeries]:
    """rows: (account_id, normalized_desc, posted, amount)."""
    groups: dict[tuple[str, str, int], list[tuple[date, float]]] = {}
    for account_id, norm, posted, amount in rows:
        key = (account_id, merchant_key(norm), 1 if amount >= 0 else -1)
        if not key[1]:
            continue
        groups.setdefault(key, []).append((posted, amount))

    out: list[RecurringSeries] = []
    for (account_id, merchant, sign), items in groups.items():
        items.sort()
        dates = [d for d, _ in items]
        classified = _classify_intervals(dates)
        if not classified:
            continue
        cadence, nominal = classified
        amounts = [a for _, a in items]
        avg = sum(amounts) / len(amounts)
        # amounts should be broadly stable (±30% of the median magnitude)
        med_amt = median(abs(a) for a in amounts)
        if med_amt > 0 and any(abs(abs(a) - med_amt) > 0.30 * med_amt for a in amounts):
            continue
        last = dates[-1]
        out.append(
            RecurringSeries(
                merchant=merchant,
                cadence=cadence,
                avg_amount=round(avg, 2),
                last_date=last,
                next_date=last + timedelta(days=nominal),
                count=len(items),
                account_id=account_id,
                is_bill=sign < 0,
            )
        )
    out.sort(key=lambda s: s.next_date)
    return out


def detect_recurring(session: Session, lookback_days: int = 400) -> list[RecurringSeries]:
    since = date.today() - timedelta(days=lookback_days)
    rows = session.execute(
        select(
            Transaction.account_id,
            Transaction.normalized_desc,
            Transaction.posted,
            Transaction.amount,
        ).where(Transaction.posted >= since, Transaction.pending.is_(False))
    ).all()
    return detect_recurring_from_rows([tuple(r) for r in rows])
