"""Household goals: a named target, a linked account, and honest progress math.

A goal is deliberately thin — name, target amount, optional deadline, and the
account whose balance IS the measurement. Progress is derived, never stored, so
it can never drift from the ledger.

Two directions of travel:
  savings / purchase / other : progress = balance - starting_amount
  debt_payoff                : progress = starting_amount - |balance|

Pace comes from BalanceSnapshot history when there is enough of it (the same
series that feeds net_worth_history), and falls back to the coarse
progress-since-creation average otherwise. Every result carries a `basis`
string saying exactly how it was computed, and on_track is None — never a
guess — whenever the inputs cannot support a verdict.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .models import Account, Base, BalanceSnapshot, _uid

CATEGORIES = ("savings", "debt_payoff", "purchase", "other")
STATUSES = ("active", "achieved", "abandoned")

#: Debt payoff counts *down* a liability; everything else counts *up*.
_DEBT_CATEGORIES = ("debt_payoff",)

#: Snapshot history shorter than this can't produce a trustworthy monthly pace.
MIN_PACE_DAYS = 21
DAYS_PER_MONTH = 30.44
#: Observed pace within this fraction of the required pace still counts as on track.
PACE_TOLERANCE = 0.95


class Goal(Base):
    """A household savings/payoff/purchase target measured against an account."""

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("goal"))
    name: Mapped[str] = mapped_column(String(160))
    target_amount: Mapped[float] = mapped_column(Float)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    category: Mapped[str] = mapped_column(String(20), default="savings", index=True)
    linked_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    starting_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    note: Mapped[str] = mapped_column(Text, default="")


def _is_debt(goal: Goal) -> bool:
    return goal.category in _DEBT_CATEGORIES


def _measure(goal: Goal, balance: float) -> float:
    """Progress toward the target implied by one balance reading."""
    if _is_debt(goal):
        return goal.starting_amount - abs(balance)
    return balance - goal.starting_amount


def _months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / DAYS_PER_MONTH


def _created_date(goal: Goal) -> date:
    created = goal.created_at or datetime.utcnow()
    return created.date() if isinstance(created, datetime) else created


def _observed_pace(
    session: Session, goal: Goal, today: date, balance: float | None
) -> tuple[float | None, str]:
    """Monthly progress rate: snapshot history first, goal age as a coarse fallback.

    Returns (pace_per_month, description). pace is None whenever the history is
    too short to say anything honest — the caller then reports on_track as None.
    """
    created = _created_date(goal)
    if goal.linked_account_id:
        snaps = (
            session.execute(
                select(BalanceSnapshot)
                .where(
                    BalanceSnapshot.account_id == goal.linked_account_id,
                    BalanceSnapshot.date >= created,
                )
                .order_by(BalanceSnapshot.date)
            )
            .scalars()
            .all()
        )
        if len(snaps) >= 2:
            first, last = snaps[0], snaps[-1]
            span_days = (last.date - first.date).days
            if span_days >= MIN_PACE_DAYS:
                delta = _measure(goal, last.balance) - _measure(goal, first.balance)
                pace = delta / (span_days / DAYS_PER_MONTH)
                return pace, (
                    f"observed pace from {len(snaps)} balance snapshots spanning "
                    f"{span_days} days ({first.date.isoformat()} to {last.date.isoformat()})"
                )

    age_days = (today - created).days
    if balance is not None and age_days >= MIN_PACE_DAYS:
        pace = _measure(goal, balance) / (age_days / DAYS_PER_MONTH)
        return pace, (
            f"coarse pace — no usable snapshot history, so progress is averaged over the "
            f"{age_days} days since the goal was created"
        )
    return None, (
        f"no observed pace yet — the goal is {max(0, age_days)} days old and there is no "
        f"balance history spanning {MIN_PACE_DAYS}+ days"
    )


def goal_progress(session: Session, goal: Goal, today: date | None = None) -> dict:
    """Where a goal stands, and whether its pace clears its deadline.

    Every field that cannot be computed honestly comes back None, and `basis`
    always states what the numbers were derived from.
    """
    today = today or date.today()
    account = session.get(Account, goal.linked_account_id) if goal.linked_account_id else None
    balance = account.balance if account else None

    result: dict = {
        "goal_id": goal.id,
        "name": goal.name,
        "category": goal.category,
        "status": goal.status,
        "target_amount": round(goal.target_amount, 2),
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "starting_amount": round(goal.starting_amount, 2),
        "linked_account_id": goal.linked_account_id,
        "linked_account": account.name if account else None,
        "current_balance": round(balance, 2) if balance is not None else None,
        "current_amount": None,
        "remaining": None,
        "percent_complete": None,
        "required_monthly": None,
        "observed_monthly": None,
        "months_remaining": None,
        "on_track": None,
        "basis": "",
    }

    if account is None or balance is None:
        result["basis"] = (
            "no linked account with a balance — progress cannot be measured; link an "
            "account or track this goal manually"
        )
        return result

    current = _measure(goal, balance)
    remaining = goal.target_amount - current
    direction = (
        f"debt payoff: starting {goal.starting_amount:,.2f} - |balance| {abs(balance):,.2f}"
        if _is_debt(goal)
        else f"savings: balance {balance:,.2f} - starting {goal.starting_amount:,.2f}"
    )
    result["current_amount"] = round(current, 2)
    result["remaining"] = round(max(0.0, remaining), 2)
    # Not clamped: <0 means the household is moving the wrong way, >100 means
    # they overshot. Hiding either would be dishonest.
    result["percent_complete"] = round(current / goal.target_amount * 100, 1)

    observed, pace_basis = _observed_pace(session, goal, today, balance)
    result["observed_monthly"] = round(observed, 2) if observed is not None else None

    parts = [f"{direction} = {current:,.2f} of {goal.target_amount:,.2f} target", pace_basis]

    if current >= goal.target_amount:
        result["on_track"] = True
        result["basis"] = "; ".join(
            parts + ["target already reached — mark it achieved"]
        )
        return result

    if goal.target_date is None:
        result["basis"] = "; ".join(
            parts + ["no target date, so there is nothing to be on or off track against"]
        )
        return result

    months_left = _months_between(today, goal.target_date)
    result["months_remaining"] = round(months_left, 1)
    if months_left <= 0:
        result["on_track"] = False
        result["basis"] = "; ".join(
            parts + [f"target date {goal.target_date.isoformat()} has passed with "
                     f"{remaining:,.2f} still to go"]
        )
        return result

    required = remaining / months_left
    result["required_monthly"] = round(required, 2)
    if observed is None:
        result["basis"] = "; ".join(
            parts + [f"needs {required:,.2f}/month for {months_left:.1f} months, but pace is "
                     "unknown so on_track is undetermined"]
        )
        return result

    result["on_track"] = observed >= required * PACE_TOLERANCE
    verdict = "clears" if result["on_track"] else "falls short of"
    result["basis"] = "; ".join(
        parts + [f"observed {observed:,.2f}/month {verdict} the {required:,.2f}/month needed "
                 f"over {months_left:.1f} remaining months"]
    )
    return result


def create_goal(
    session: Session,
    *,
    name: str,
    target_amount: float,
    category: str = "savings",
    target_date: date | None = None,
    linked_account_id: str | None = None,
    starting_amount: float | None = None,
    note: str = "",
    today: date | None = None,
) -> Goal:
    """Create a goal, validating everything the progress math depends on.

    starting_amount defaults to the linked account's balance today (its absolute
    value for a debt payoff), so progress counts from now rather than from zero.
    """
    today = today or date.today()
    name = (name or "").strip()
    if not name:
        raise ValueError("goal name is required")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    try:
        target_amount = float(target_amount)
    except (TypeError, ValueError):
        raise ValueError("target_amount must be a number") from None
    if target_amount <= 0:
        raise ValueError("target_amount must be greater than zero")
    if target_date is not None and target_date <= today:
        raise ValueError("target_date must be in the future")

    account = None
    if linked_account_id:
        account = session.get(Account, linked_account_id)
        if account is None:
            raise ValueError(f"no account {linked_account_id} — link an existing account")

    if starting_amount is None:
        if account is not None and account.balance is not None:
            starting_amount = (
                abs(account.balance) if category in _DEBT_CATEGORIES else account.balance
            )
        else:
            starting_amount = 0.0

    goal = Goal(
        name=name,
        target_amount=target_amount,
        category=category,
        target_date=target_date,
        linked_account_id=linked_account_id,
        starting_amount=float(starting_amount),
        note=note or "",
    )
    session.add(goal)
    session.flush()
    return goal


def update_goal_status(session: Session, goal_id: str, status: str) -> Goal:
    """Move a goal between active / achieved / abandoned."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise ValueError(f"no goal {goal_id} — call list_goals_with_progress for ids")
    goal.status = status
    session.flush()
    return goal


def list_goals_with_progress(
    session: Session, status: str | None = "active", today: date | None = None
) -> list[dict]:
    """Every goal (default: only active ones) with its computed progress."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)} (or None for all)")
    query = select(Goal).order_by(Goal.created_at)
    if status is not None:
        query = query.where(Goal.status == status)
    goals = session.execute(query).scalars().all()
    return [goal_progress(session, goal, today=today) for goal in goals]
