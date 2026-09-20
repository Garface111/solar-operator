"""CSV statement import with column auto-detection.

Handles the common bank-export shapes:
- date / description / amount            (signed amount)
- date / description / debit / credit    (separate columns)
Header names are matched loosely, dates in several formats, amounts with $ , ( ).

Apple Card (Wallet) exports are the one shape whose signs arrive inverted:
Apple writes charges POSITIVE and payments/refunds NEGATIVE — the card's
perspective, not the cardholder's. Everything here stores money-out as
negative, so keeping Apple's raw signs turns every coffee into income.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..ingest import IngestResult, TxnIn, ingest_transactions, upsert_account
from ..models import Account

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y")

_DATE_KEYS = ("date", "posted", "transaction date", "post date", "posting date")
_DESC_KEYS = ("description", "payee", "merchant", "name", "memo", "details")
_AMOUNT_KEYS = ("amount", "transaction amount")
_DEBIT_KEYS = ("debit", "withdrawal", "withdrawals", "money out")
_CREDIT_KEYS = ("credit", "deposit", "deposits", "money in")

_AMOUNT_CLEAN = re.compile(r"[$,\s]")

#: Wallet always exports this exact header set (plus Description/Category/Type/
#: Purchased By). Requiring the trio keeps the signature tight enough that no
#: bank CSV trips it — "Clearing Date" alongside "Amount (USD)" is Apple's alone.
_APPLE_CARD_COLUMNS = frozenset({"transaction date", "clearing date", "amount (usd)"})


def is_apple_card_export(fieldnames: list[str]) -> bool:
    return _APPLE_CARD_COLUMNS <= {f.lower().strip() for f in fieldnames}


def parse_amount(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = _AMOUNT_CLEAN.sub("", raw.strip("()"))
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -abs(value) if negative else value


def parse_date(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _find(fieldnames: list[str], keys: tuple[str, ...]) -> str | None:
    lowered = {f.lower().strip(): f for f in fieldnames}
    for key in keys:
        if key in lowered:
            return lowered[key]
    for key in keys:  # loose contains-match fallback
        for low, orig in lowered.items():
            if key in low:
                return orig
    return None


def parse_csv(text: str) -> list[TxnIn]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    date_col = _find(reader.fieldnames, _DATE_KEYS)
    desc_col = _find(reader.fieldnames, _DESC_KEYS)
    amount_col = _find(reader.fieldnames, _AMOUNT_KEYS)
    debit_col = _find(reader.fieldnames, _DEBIT_KEYS)
    credit_col = _find(reader.fieldnames, _CREDIT_KEYS)
    if not date_col or not desc_col or not (amount_col or debit_col or credit_col):
        raise ValueError(
            f"Could not detect columns in header {reader.fieldnames}; "
            "need date + description + amount (or debit/credit)"
        )
    apple = is_apple_card_export(reader.fieldnames)
    txns: list[TxnIn] = []
    for row in reader:
        posted = parse_date(row.get(date_col) or "")
        if posted is None:
            continue
        amount: float | None = None
        if amount_col:
            amount = parse_amount(row.get(amount_col) or "")
        if amount is None and (debit_col or credit_col):
            debit = parse_amount(row.get(debit_col) or "") if debit_col else None
            credit = parse_amount(row.get(credit_col) or "") if credit_col else None
            if debit is not None and debit != 0:
                amount = -abs(debit)
            elif credit is not None:
                amount = abs(credit)
        if amount is None:
            continue
        if apple and amount != 0:
            amount = -amount
        txns.append(TxnIn(posted=posted, amount=amount, description=(row.get(desc_col) or "").strip()))
    return txns


def import_csv(
    session: Session,
    *,
    text: str,
    account_name: str,
    kind: str = "checking",
    owner: str = "joint",
    institution: str = "",
    account: Account | None = None,
) -> IngestResult:
    """Pass `account` to import into a specific existing account; otherwise one
    is found-or-created by (source='csv', name) — which will NOT match a
    same-named account another source created."""
    txns = parse_csv(text)
    if account is None:
        account = upsert_account(
            session,
            source="csv",
            name=account_name,
            kind=kind,
            owner=owner,
            institution=institution,
        )
    return ingest_transactions(session, account, txns)
