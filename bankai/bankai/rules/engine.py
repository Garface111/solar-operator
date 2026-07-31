"""Rules engine: evaluates enabled rules against the finance model and records
firings. Every firing has a dedupe_key with a unique index, so a rule can never
double-send for the same trigger no matter how often the scheduler runs."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..intelligence.insights import spending_summary, upcoming_bills
from ..models import Account, Rule, RuleFiring, Transaction


def evaluate_rules(session: Session, now: datetime | None = None) -> list[RuleFiring]:
    now = now or datetime.utcnow()
    fired: list[RuleFiring] = []
    rules = session.execute(select(Rule).where(Rule.enabled.is_(True))).scalars().all()
    for rule in rules:
        handler = _HANDLERS.get(rule.kind)
        if not handler:
            continue
        for dedupe_key, subject, body in handler(session, rule, now):
            firing = _record(session, rule, dedupe_key, subject, body)
            if firing:
                fired.append(firing)
    return fired


def _record(
    session: Session, rule: Rule, dedupe_key: str, subject: str, body: str
) -> RuleFiring | None:
    exists = session.execute(
        select(RuleFiring.id).where(
            RuleFiring.rule_id == rule.id, RuleFiring.dedupe_key == dedupe_key
        )
    ).scalar_one_or_none()
    if exists:
        return None
    firing = RuleFiring(rule_id=rule.id, dedupe_key=dedupe_key, subject=subject, body=body)
    session.add(firing)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return firing


def _reminder(session: Session, rule: Rule, now: datetime):
    params = rule.params or {}
    today = now.date()
    day_of_month = params.get("day_of_month")
    weekday = params.get("weekday")
    due = (day_of_month is not None and today.day == int(day_of_month)) or (
        weekday is not None and today.weekday() == int(weekday)
    )
    if due:
        yield today.isoformat(), f"Reminder: {rule.name}", rule.message or rule.name


def _balance_below(session: Session, rule: Rule, now: datetime):
    params = rule.params or {}
    threshold = float(params.get("threshold", 0))
    account_id = params.get("account_id")
    query = select(Account)
    if account_id:
        query = query.where(Account.id == account_id)
    for account in session.execute(query).scalars():
        if account.balance is not None and account.balance < threshold:
            yield (
                f"{account.id}|{now.date().isoformat()}",
                f"Low balance: {account.name} at ${account.balance:,.2f}",
                (rule.message or "")
                + f"\n{account.name} ({account.institution}) is at ${account.balance:,.2f}, "
                f"below your ${threshold:,.2f} threshold.",
            )


def _large_transaction(session: Session, rule: Rule, now: datetime):
    params = rule.params or {}
    threshold = abs(float(params.get("threshold", 500)))
    since = now.date() - timedelta(days=7)
    txns = (
        session.execute(
            select(Transaction).where(
                Transaction.posted >= since,
                Transaction.category != "transfer",
            )
        )
        .scalars()
        .all()
    )
    for txn in txns:
        if abs(txn.amount) >= threshold:
            yield (
                txn.fingerprint,
                f"Large transaction: {txn.description[:60]} ${abs(txn.amount):,.2f}",
                (rule.message or "")
                + f"\n{txn.posted} — {txn.description} — ${txn.amount:,.2f}",
            )


def _bill_reminder(session: Session, rule: Rule, now: datetime):
    params = rule.params or {}
    days_before = int(params.get("days_before", 3))
    for bill in upcoming_bills(session, days=days_before):
        yield (
            f"{bill['merchant']}|{bill['expected_date']}",
            f"Bill due soon: {bill['merchant']} (~${abs(bill['avg_amount']):,.2f})",
            (rule.message or "")
            + f"\n{bill['merchant']} is expected around {bill['expected_date']} "
            f"(~${abs(bill['avg_amount']):,.2f}, {bill['cadence']}).",
        )


def _weekly_digest(session: Session, rule: Rule, now: datetime):
    params = rule.params or {}
    weekday = int(params.get("weekday", 0))
    today = now.date()
    if today.weekday() != weekday:
        return
    since = today - timedelta(days=7)
    summary = spending_summary(session, since, today + timedelta(days=1))
    lines = [
        f"Week of {since} – {today}",
        f"Income: ${summary['income']:,.2f}   Spend: ${abs(summary['spend']):,.2f}   "
        f"Net: ${summary['net']:,.2f}",
        "",
        "Top categories:",
    ]
    for row in summary["by_category"][:6]:
        if row["total"] < 0:
            lines.append(f"  {row['category']}: ${abs(row['total']):,.2f} ({row['count']} txns)")
    bills = upcoming_bills(session, days=10)
    if bills:
        lines += ["", "Coming up:"]
        for bill in bills[:8]:
            lines.append(
                f"  {bill['expected_date']}  {bill['merchant']}  ~${abs(bill['avg_amount']):,.2f}"
            )
    yield today.isoformat(), "Weekly household finance digest", "\n".join(lines)


_HANDLERS = {
    "reminder": _reminder,
    "balance_below": _balance_below,
    "large_transaction": _large_transaction,
    "bill_reminder": _bill_reminder,
    "weekly_digest": _weekly_digest,
}

RULE_KINDS = sorted(_HANDLERS)
