"""Shared ingestion: normalization, fingerprinting, idempotent upserts.

All connectors funnel through here so dedupe behaves identically regardless of
whether a transaction arrived via SimpleFIN or a CSV re-import.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .intelligence.categorize import categorize
from .models import Account, BalanceSnapshot, Transaction

_WS = re.compile(r"\s+")
# strip #/* separators and any token containing a 3+ digit run (card/ref numbers)
_NOISE = re.compile(r"[#*]|\b\w*\d{3,}\w*\b")

# Manually tracked assets/liabilities (source="manual"): the home, its mortgage,
# vehicles, private loans — anything no bank feed reports. They sum into net worth
# exactly like synced accounts, so home equity = property value + (negative) mortgage.
MANUAL_ASSET_KINDS = ["property", "vehicle", "other"]
#: credit belongs here because some cards have no feed at all — the Apple Card
#: is readable only from a statement — and a card left out of the list does not
#: read as "unknown", it reads as a card with nothing owed.
MANUAL_LIABILITY_KINDS = ["mortgage", "loan", "credit"]
MANUAL_KINDS = MANUAL_ASSET_KINDS + MANUAL_LIABILITY_KINDS


def normalize_manual_balance(kind: str, balance: float) -> float:
    """Users enter liabilities as the amount owed; net worth needs them negative.
    Assets are forced positive, liabilities negative; 'other' keeps its sign."""
    if kind in MANUAL_LIABILITY_KINDS:
        return -abs(balance)
    if kind == "other":
        return balance
    return abs(balance)


def normalize_desc(desc: str) -> str:
    """Stable, human-ish merchant key: uppercase, strip reference numbers."""
    d = _NOISE.sub(" ", desc.upper())
    return _WS.sub(" ", d).strip()[:200]


@dataclass
class TxnIn:
    posted: date
    amount: float
    description: str
    external_id: str | None = None
    pending: bool = False


@dataclass
class IngestResult:
    added: int = 0
    skipped: int = 0
    ids: list[str] = field(default_factory=list)


def fingerprint(account_id: str, txn: TxnIn, occurrence: int = 0) -> str:
    if txn.external_id:
        raw = f"{account_id}|ext|{txn.external_id}"
    else:
        raw = (
            f"{account_id}|{txn.posted.isoformat()}|{txn.amount:.2f}"
            f"|{normalize_desc(txn.description)}|{occurrence}"
        )
    return hashlib.sha256(raw.encode()).hexdigest()


def upsert_account(
    session: Session,
    *,
    source: str,
    name: str,
    external_id: str | None = None,
    institution: str = "",
    kind: str = "checking",
    owner: str = "joint",
    currency: str = "USD",
    balance: float | None = None,
    balance_date: datetime | None = None,
) -> Account:
    account = None
    if external_id is not None:
        account = session.execute(
            select(Account).where(Account.source == source, Account.external_id == external_id)
        ).scalar_one_or_none()
    if account is None:
        account = session.execute(
            select(Account).where(Account.source == source, Account.name == name)
        ).scalar_one_or_none()
    if account is None:
        account = Account(source=source, external_id=external_id, name=name)
        session.add(account)
    account.institution = institution or account.institution
    account.kind = kind or account.kind
    account.owner = account.owner or owner
    account.currency = currency or account.currency
    if balance is not None:
        account.balance = balance
        account.balance_date = balance_date or datetime.utcnow()
        session.flush()
        _snapshot_balance(session, account)
    session.flush()
    return account


def _snapshot_balance(session: Session, account: Account) -> None:
    today = (account.balance_date or datetime.utcnow()).date()
    existing = session.execute(
        select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == today
        )
    ).scalar_one_or_none()
    if existing:
        existing.balance = account.balance or 0.0
    else:
        session.add(
            BalanceSnapshot(account_id=account.id, date=today, balance=account.balance or 0.0)
        )


def ingest_transactions(session: Session, account: Account, txns: list[TxnIn]) -> IngestResult:
    """Insert transactions idempotently. Identical same-day duplicates within one
    batch get distinct occurrence indices so both survive (two coffees, one day) while
    a re-import of the same batch is still a no-op."""
    result = IngestResult()
    batch_counts: dict[str, int] = {}
    for txn in txns:
        base_key = f"{txn.posted}|{txn.amount:.2f}|{normalize_desc(txn.description)}"
        occurrence = batch_counts.get(base_key, 0)
        batch_counts[base_key] = occurrence + 1
        fp = fingerprint(account.id, txn, occurrence)
        exists = session.execute(
            select(Transaction.id).where(Transaction.fingerprint == fp)
        ).scalar_one_or_none()
        if exists:
            result.skipped += 1
            continue
        row = Transaction(
            account_id=account.id,
            external_id=txn.external_id,
            posted=txn.posted,
            amount=round(txn.amount, 2),
            description=txn.description.strip(),
            normalized_desc=normalize_desc(txn.description),
            category=categorize(txn.description, txn.amount),
            pending=txn.pending,
            fingerprint=fp,
        )
        session.add(row)
        session.flush()
        result.added += 1
        result.ids.append(row.id)
    return result
