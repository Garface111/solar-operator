"""What a statement says beyond the balance: what's due, and when.

A balance alone cannot answer "can we cover the cards this month" — that needs
the due date and the minimum. Bank feeds do not carry either, and the accounts
that most need them are exactly the ones with no feed at all: the Apple Card is
readable only from a statement PDF a human forwards.

Its own table rather than columns on Account, because `create_all` adds new
tables to a live database but never new columns, and this shipped after the
household's data was already in there.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .models import Base, _uid


class AccountTerms(Base):
    """The obligation attached to an account, as of a named statement."""

    __tablename__ = "account_terms"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("term"))
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id"), unique=True, index=True
    )
    #: The full amount owed per the statement — for Apple Card this is "Total
    #: Balance", which includes remaining Monthly Installments, not the smaller
    #: "Monthly Balance" that omits them.
    statement_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    minimum_payment: Mapped[float | None] = mapped_column(Float, nullable=True)
    payment_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    apr: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The statement date these figures came from — without it a stale number
    #: looks current, which is the whole failure mode this exists to prevent.
    as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


def set_terms(
    session: Session,
    account_id: str,
    *,
    statement_balance: float | None = None,
    minimum_payment: float | None = None,
    payment_due_date: date | None = None,
    apr: float | None = None,
    as_of: date | None = None,
    source: str = "",
) -> AccountTerms:
    """Upsert one account's terms. Only fields given are changed, so recording a
    new due date never silently wipes an APR read from an older statement."""
    row = session.execute(
        select(AccountTerms).where(AccountTerms.account_id == account_id)
    ).scalar_one_or_none()
    if row is None:
        row = AccountTerms(account_id=account_id)
        session.add(row)
    for field, value in (
        ("statement_balance", statement_balance),
        ("minimum_payment", minimum_payment),
        ("payment_due_date", payment_due_date),
        ("apr", apr),
        ("as_of", as_of),
    ):
        if value is not None:
            setattr(row, field, value)
    if source:
        row.source = source
    session.flush()
    return row


def terms_by_account(session: Session) -> dict[str, dict]:
    """Everything known about what is owed, keyed by account id, for the UI."""
    out: dict[str, dict] = {}
    for row in session.execute(select(AccountTerms)).scalars():
        due = row.payment_due_date
        out[row.account_id] = {
            "statement_balance": row.statement_balance,
            "minimum_payment": row.minimum_payment,
            "payment_due_date": due.isoformat() if due else None,
            "days_until_due": (due - date.today()).days if due else None,
            "apr": row.apr,
            "as_of": row.as_of.isoformat() if row.as_of else None,
            "source": row.source,
            # A due date in the past means the statement is stale, not that the
            # payment is overdue — say which so nobody panics at old data.
            "stale": bool(due and due < date.today()),
        }
    return out
