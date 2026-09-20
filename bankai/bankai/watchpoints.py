"""Watchpoints: the copilot's time-native cognition.

A rule is a *notification* — it fires and emails a fixed message. A watchpoint is
a **flag planted for future-self**: the copilot writes down what it wants to
reconsider and why, arms a condition, and months later — when the date arrives or
the number crosses — the flag fires and wakes the copilot up IN THE SHARED THREAD
as a real agent turn, so it reasons about the trigger with full current context
(fresh balances, documents, memory) rather than replaying a canned sentence.

Each watchpoint fires AT MOST ONCE: `status` is the guard (armed -> fired), so
`evaluate_watchpoints` is safe to call on every scheduler tick.

The wake mechanism itself lives in the scheduler (see INTEGRATION-watchpoints.md):
`build_wake_prompt()` produces the nudge text, which is posted into the shared
conversation exactly the way the monthly review is —
`bankai.messaging.thread.handle_web(session, speaker, prompt)`.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .intelligence.insights import net_worth
from .models import Account, Base, _uid

#: Account kinds counted as spendable cash by the `liquid_below` condition.
LIQUID_KINDS = ("checking", "savings")

STATUS_ARMED = "armed"
STATUS_FIRED = "fired"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_ARMED, STATUS_FIRED, STATUS_CANCELLED)


class Watchpoint(Base):
    """A flag the copilot plants today for the copilot of months from now.

    Defined in this module rather than models.py so the feature is self-contained;
    importing `bankai.watchpoints` anywhere before `Base.metadata.create_all()`
    registers the table.
    """

    __tablename__ = "watchpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("wp"))
    title: Mapped[str] = mapped_column(String(200))
    #: What future-self should reconsider, and why it mattered when it was set.
    note: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(30))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(20), default="agent")  # agent | user
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default=STATUS_ARMED, index=True)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: Transient (not a column): the measured reading that tripped this watchpoint,
    #: attached by evaluate_watchpoints so the wake prompt can quote real numbers.
    #: build_wake_prompt falls back to describe_condition() when it is absent.
    trigger_detail = ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Watchpoint {self.id} {self.status} {self.kind} {self.title!r}>"


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _float(kind: str, params: dict, key: str) -> float:
    if key not in params or params[key] is None:
        raise ValueError(f"watchpoint kind {kind!r} requires a numeric {key!r}")
    try:
        return float(params[key])
    except (TypeError, ValueError):
        raise ValueError(f"watchpoint kind {kind!r} requires a numeric {key!r}") from None


def _validate_on_date(params: dict) -> dict:
    raw = params.get("date")
    if not raw:
        raise ValueError("watchpoint kind 'on_date' requires an ISO 'date' (YYYY-MM-DD)")
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return {"date": raw.isoformat()}
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise ValueError(f"watchpoint 'date' is not an ISO date: {raw!r}") from None
    return {"date": parsed.isoformat()}


def _validate_threshold(kind: str, params: dict) -> dict:
    return {"threshold": _float(kind, params, "threshold")}


def _validate_account_balance_below(params: dict) -> dict:
    account_id = str(params.get("account_id") or "").strip()
    if not account_id:
        raise ValueError("watchpoint kind 'account_balance_below' requires an 'account_id'")
    return {
        "account_id": account_id,
        "threshold": _float("account_balance_below", params, "threshold"),
    }


def _check_on_date(session: Session, wp: Watchpoint, now: datetime) -> str | None:
    due = date.fromisoformat(wp.params["date"])
    if now.date() < due:
        return None
    return f"the date you set ({due.isoformat()}) has arrived — today is {now.date().isoformat()}"


def _check_net_worth_below(session: Session, wp: Watchpoint, now: datetime) -> str | None:
    snapshot = net_worth(session)
    if not snapshot["accounts"]:
        return None  # nothing measurable yet; never fire on an empty model
    threshold = float(wp.params["threshold"])
    total = snapshot["total"]
    if total >= threshold:
        return None
    return f"net worth is {_money(total)}, below the {_money(threshold)} you flagged"


def _check_net_worth_above(session: Session, wp: Watchpoint, now: datetime) -> str | None:
    snapshot = net_worth(session)
    if not snapshot["accounts"]:
        return None
    threshold = float(wp.params["threshold"])
    total = snapshot["total"]
    if total <= threshold:
        return None
    return f"net worth is {_money(total)}, above the {_money(threshold)} you flagged"


def _check_account_balance_below(session: Session, wp: Watchpoint, now: datetime) -> str | None:
    account = session.get(Account, wp.params["account_id"])
    if account is None or account.balance is None:
        return None  # account gone or never valued: unmeasurable, stay armed
    threshold = float(wp.params["threshold"])
    if account.balance >= threshold:
        return None
    return (
        f"{account.name} is at {_money(account.balance)}, "
        f"below the {_money(threshold)} you flagged"
    )


def _liquid_accounts(session: Session) -> list[Account]:
    return [
        account
        for account in session.execute(
            select(Account).where(Account.kind.in_(LIQUID_KINDS))
        ).scalars()
        if account.balance is not None
    ]


def _check_liquid_below(session: Session, wp: Watchpoint, now: datetime) -> str | None:
    accounts = _liquid_accounts(session)
    if not accounts:
        return None  # no checking/savings on file: unmeasurable, stay armed
    threshold = float(wp.params["threshold"])
    total = sum(account.balance or 0.0 for account in accounts)
    if total >= threshold:
        return None
    names = ", ".join(account.name for account in accounts)
    return (
        f"liquid cash (checking + savings: {names}) is {_money(total)}, "
        f"below the {_money(threshold)} you flagged"
    )


_VALIDATORS = {
    "on_date": _validate_on_date,
    "net_worth_below": lambda p: _validate_threshold("net_worth_below", p),
    "net_worth_above": lambda p: _validate_threshold("net_worth_above", p),
    "account_balance_below": _validate_account_balance_below,
    "liquid_below": lambda p: _validate_threshold("liquid_below", p),
}

_CHECKS = {
    "on_date": _check_on_date,
    "net_worth_below": _check_net_worth_below,
    "net_worth_above": _check_net_worth_above,
    "account_balance_below": _check_account_balance_below,
    "liquid_below": _check_liquid_below,
}

WATCHPOINT_KINDS = sorted(_CHECKS)


def validate_condition(kind: str, params: dict | None) -> dict:
    """Normalize + validate a condition. Raises ValueError on anything unusable."""
    validator = _VALIDATORS.get(kind)
    if validator is None:
        raise ValueError(
            f"unknown watchpoint kind {kind!r} — valid kinds: {', '.join(WATCHPOINT_KINDS)}"
        )
    return validator(params or {})


def describe_condition(watchpoint: Watchpoint, session: Session | None = None) -> str:
    """Human phrasing of what this watchpoint is waiting for. Pass a session to
    name the account instead of showing its raw id."""
    params = watchpoint.params or {}
    kind = watchpoint.kind
    if kind == "on_date":
        return f"the date {params.get('date')} arrives"
    if kind == "net_worth_below":
        return f"net worth falls below {_money(float(params.get('threshold', 0)))}"
    if kind == "net_worth_above":
        return f"net worth rises above {_money(float(params.get('threshold', 0)))}"
    if kind == "account_balance_below":
        label = str(params.get("account_id") or "")
        if session is not None:
            account = session.get(Account, label)
            if account:
                label = account.name
        return f"{label} falls below {_money(float(params.get('threshold', 0)))}"
    if kind == "liquid_below":
        return f"liquid cash falls below {_money(float(params.get('threshold', 0)))}"
    return f"{kind} {params}"


def create_watchpoint(
    session: Session,
    *,
    title: str,
    kind: str,
    note: str = "",
    params: dict | None = None,
    created_by: str = "agent",
) -> Watchpoint:
    """Plant a flag. Validates the condition before anything is written."""
    title = (title or "").strip()
    if not title:
        raise ValueError("watchpoint requires a title")
    clean_params = validate_condition(kind, params)
    watchpoint = Watchpoint(
        title=title[:200],
        note=(note or "").strip(),
        kind=kind,
        params=clean_params,
        created_by=created_by or "agent",
        status=STATUS_ARMED,
    )
    session.add(watchpoint)
    session.flush()
    return watchpoint


def cancel_watchpoint(session: Session, watchpoint_id: str) -> Watchpoint:
    """Disarm a watchpoint. Idempotent for one already cancelled; a watchpoint that
    already fired cannot be cancelled (its wake is history, not a pending event)."""
    watchpoint = session.get(Watchpoint, watchpoint_id)
    if watchpoint is None:
        raise ValueError(f"no watchpoint with id {watchpoint_id!r}")
    if watchpoint.status == STATUS_FIRED:
        raise ValueError(f"watchpoint {watchpoint_id!r} already fired; nothing to cancel")
    watchpoint.status = STATUS_CANCELLED
    session.flush()
    return watchpoint


def list_watchpoints(session: Session, status: str | None = None) -> list[Watchpoint]:
    """All watchpoints, newest first. Pass status to filter (armed|fired|cancelled)."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r} — valid: {', '.join(STATUSES)}")
    query = select(Watchpoint).order_by(Watchpoint.created_at.desc(), Watchpoint.id.desc())
    if status is not None:
        query = query.where(Watchpoint.status == status)
    return list(session.execute(query).scalars().all())


def evaluate_watchpoints(session: Session, now: datetime | None = None) -> list[Watchpoint]:
    """Evaluate every armed watchpoint; return the ones that just fired.

    Firing flips status to 'fired' and stamps fired_at, so a watchpoint can wake the
    copilot at most once no matter how often this runs. Conditions that cannot be
    measured yet (missing account, empty finance model) leave the watchpoint armed.
    """
    now = now or datetime.utcnow()
    fired: list[Watchpoint] = []
    armed = (
        session.execute(
            select(Watchpoint)
            .where(Watchpoint.status == STATUS_ARMED)
            .order_by(Watchpoint.created_at, Watchpoint.id)
        )
        .scalars()
        .all()
    )
    for watchpoint in armed:
        check = _CHECKS.get(watchpoint.kind)
        if check is None:
            continue  # unknown kind (older row / hand-edited): never fires
        detail = check(session, watchpoint, now)
        if not detail:
            continue
        watchpoint.status = STATUS_FIRED
        watchpoint.fired_at = now
        watchpoint.trigger_detail = detail
        fired.append(watchpoint)
    if fired:
        session.flush()
    return fired


def build_wake_prompt(watchpoint: Watchpoint) -> str:
    """The nudge injected into the shared thread as a user-role turn when a
    watchpoint fires — it wakes the copilot with its own past reasoning attached."""
    set_on = watchpoint.created_at.date().isoformat() if watchpoint.created_at else "an earlier date"
    detail = getattr(watchpoint, "trigger_detail", "") or describe_condition(watchpoint)
    note = (watchpoint.note or "").strip() or "(no note recorded)"
    return (
        f"[watchpoint fired] '{watchpoint.title}' — you set this on {set_on} with note: "
        f"{note}. Current condition: {detail}. Reassess with fresh data and tell the "
        "household what, if anything, they should do now. If it still needs watching, "
        "plant a new watchpoint; if it is settled, say so and close the loop."
    )
