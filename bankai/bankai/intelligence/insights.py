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


def _valued_accounts(session: Session) -> list[Account]:
    """Every account that carries a usable balance — the exact set net worth sums,
    and the exact set the daily snapshot must stamp. Feed and manual alike: a manual
    property or mortgage counts no differently from a synced checking account, and
    none is excluded for having been last edited long ago. net_worth() and
    snapshot_net_worth() both go through here so the live total and the daily
    snapshot can never diverge on *which* accounts they include."""
    return [a for a in session.execute(select(Account)).scalars() if a.balance is not None]


def net_worth(session: Session) -> dict:
    total = 0.0
    per_account = []
    for account in _valued_accounts(session):
        bal = account.balance
        total += bal
        per_account.append(
            {
                "account_id": account.id,
                "name": account.name,
                "kind": account.kind,
                "source": account.source,
                "owner": account.owner,
                "balance": round(bal, 2),
                "as_of": account.balance_date.isoformat() if account.balance_date else None,
            }
        )
    return {"total": round(total, 2), "accounts": per_account}


def snapshot_net_worth(session: Session, on: date | None = None) -> dict:
    """Stamp today's balance snapshot for EVERY valued account at once, so the daily
    net-worth history is built from the same account set — at the same current
    balances — that net_worth()/get_accounts report.

    Snapshots are otherwise written per-account only when that one account changes, so
    a day's history row would only ever sum the accounts that happened to move that
    day. Manual accounts (property, mortgage, Apple Card) change rarely, so they
    dropped out of almost every day and the total sagged by their whole value. This
    walks the full set in one pass and upserts today's row for each account, so the
    day is complete and single-basis.

    Re-running the same day re-stamps in place (no duplicate row): if a manual account
    was corrected an hour ago, calling this again brings the whole day's snapshot back
    into agreement with the accounts instead of leaving a mixed-basis row. Returns
    {"date", "total"} where total == net_worth()["total"] to the cent."""
    on = on or date.today()
    total = 0.0
    for account in _valued_accounts(session):
        total += account.balance
        existing = session.execute(
            select(BalanceSnapshot).where(
                BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == on
            )
        ).scalar_one_or_none()
        if existing:
            existing.balance = account.balance
        else:
            session.add(
                BalanceSnapshot(account_id=account.id, date=on, balance=account.balance)
            )
    session.flush()
    return {"date": on.isoformat(), "total": round(total, 2)}


def net_worth_history(session: Session, months: int = 6) -> list[dict]:
    """Daily net-worth series, reconstructed so every day reflects the SAME account
    set net_worth() sums — each account carried forward at its most recent balance on
    or before that date. A naive ``sum(snapshots where date == D)`` only ever counted
    the accounts snapshotted *on* D, so a rarely-touched manual account (a property, a
    mortgage) silently dropped out on every day it wasn't edited and the total sagged
    by its whole value. Carrying forward the last known balance repairs that from the
    sparse snapshots we already have, without inventing rows.

    A point also carries ``basis_change`` when an account first entered tracking since
    the previous point (a new property, a first mortgage). That is a real discontinuity
    in *what is being measured*, not organic wealth change, so a chart can annotate the
    step instead of drawing it as a $250k cliff. An account contributes nothing before
    its first snapshot — a value is never projected back to before it was tracked."""
    since = date.today() - timedelta(days=months * 31)
    # Full history, oldest first: carrying a balance forward into the window needs the
    # last snapshot from *before* the window too (a property valued months ago and
    # never since still counts today).
    rows = session.execute(
        select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance)
        .order_by(BalanceSnapshot.date)
    ).all()
    if not rows:
        return []

    names = {
        a.id: (a.name, a.kind)
        for a in session.execute(select(Account.id, Account.name, Account.kind)).all()
    }
    by_date: dict[date, dict[str, float]] = {}
    for account_id, d, balance in rows:
        by_date.setdefault(d, {})[account_id] = balance

    out: list[dict] = []
    carried: dict[str, float] = {}  # account_id -> most recent balance seen so far
    prev_emitted_ids: set[str] | None = None
    for d in sorted(by_date):
        carried.update(by_date[d])
        if d < since:
            continue  # fold its balances into the carry, but don't emit a pre-window point
        entered = [aid for aid in carried if prev_emitted_ids is not None and aid not in prev_emitted_ids]
        point = {"date": d.isoformat(), "total": round(sum(carried.values()), 2)}
        if entered:
            point["basis_change"] = {
                "entered": [
                    {"account_id": aid, "name": names.get(aid, (aid, ""))[0],
                     "kind": names.get(aid, ("", ""))[1], "balance": round(carried[aid], 2)}
                    for aid in entered
                ],
                # The slice of the step that is a basis change, not real movement — a
                # chart can subtract it to keep the line continuous, or label it.
                "amount": round(sum(carried[aid] for aid in entered), 2),
            }
        out.append(point)
        prev_emitted_ids = set(carried)
    return out


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
