"""CSV statement import with column auto-detection.

Handles the common bank-export shapes:
- date / description / amount            (signed amount)
- date / description / debit / credit    (separate columns)
Header names are matched loosely, dates in several formats, amounts with $ , ( ).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..ingest import IngestResult, TxnIn, ingest_transactions, upsert_account

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y")

_DATE_KEYS = ("date", "posted", "transaction date", "post date", "posting date")
_DESC_KEYS = ("description", "payee", "merchant", "name", "memo", "details")
_AMOUNT_KEYS = ("amount", "transaction amount")
_DEBIT_KEYS = ("debit", "withdrawal", "withdrawals", "money out")
_CREDIT_KEYS = ("credit", "deposit", "deposits", "money in")

_AMOUNT_CLEAN = re.compile(r"[$,\s]")


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
) -> IngestResult:
    txns = parse_csv(text)
    account = upsert_account(
        session,
        source="csv",
        name=account_name,
        kind=kind,
        owner=owner,
        institution=institution,
    )
    return ingest_transactions(session, account, txns)
