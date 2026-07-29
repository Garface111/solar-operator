"""Aggregate views over the finance model: cashflow, category spend, net worth,
upcoming bills. Everything returns plain dicts so both the API and the chat agent's
tools can serve them directly."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, BalanceSnapshot, Transaction
from .recurring import detect_recurring


def month_bounds(yyyy_mm: str) -> tuple[date, date]:
    year, month = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def spending_summary(session: Session, since: date, until: date) -> dict:
    rows = session.execute(
        select(Transaction.category, func.sum(Transaction.amount), func.count())
        .where(
            Transaction.posted >= since,
            Transaction.posted < until,
            Transaction.pending.is_(False),
            Transaction.category != "transfer",
        )
        .group_by(Transaction.category)
    ).all()
    income = sum(total for _, total, _ in rows if total and total > 0)
    spend = sum(total for _, total, _ in rows if total and total < 0)
    by_category = sorted(
        (
            {"category": cat, "total": round(total or 0.0, 2), "count": count}
            for cat, total, count in rows
        ),
        key=lambda r: r["total"],
    )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "income": round(income, 2),
        "spend": round(spend, 2),
        "net": round(income + spend, 2),
        "by_category": by_category,
        "note": "transfers excluded; spend is negative",
    }


def net_worth(session: Session) -> dict:
    accounts = session.execute(select(Account)).scalars().all()
    total = 0.0
    per_account = []
    for account in accounts:
        bal = account.balance
        if bal is None:
            continue
        total += bal
        per_account.append(
            {
                "account_id": account.id,
                "name": account.name,
                "kind": account.kind,
                "owner": account.owner,
                "balance": round(bal, 2),
                "as_of": account.balance_date.isoformat() if account.balance_date else None,
            }
        )
    return {"total": round(total, 2), "accounts": per_account}


def net_worth_history(session: Session, months: int = 6) -> list[dict]:
    since = date.today() - timedelta(days=months * 31)
    rows = session.execute(
        select(BalanceSnapshot.date, func.sum(BalanceSnapshot.balance))
        .where(BalanceSnapshot.date >= since)
        .group_by(BalanceSnapshot.date)
        .order_by(BalanceSnapshot.date)
    ).all()
    return [{"date": d.isoformat(), "total": round(total or 0.0, 2)} for d, total in rows]


def upcoming_bills(session: Session, days: int = 30) -> list[dict]:
    horizon = date.today() + timedelta(days=days)
    out = []
    for series in detect_recurring(session):
        if not series.is_bill or series.next_date > horizon:
            continue
        out.append(
            {
                "merchant": series.merchant,
                "cadence": series.cadence,
                "avg_amount": series.avg_amount,
                "expected_date": series.next_date.isoformat(),
                "last_paid": series.last_date.isoformat(),
                "seen_count": series.count,
            }
        )
    return out
