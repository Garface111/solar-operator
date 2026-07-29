"""OFX/QFX statement import.

Handles the files Bank of America, Fidelity, American Express, and Apple Card
(Wallet app -> Card Balance -> Export) produce. OFX is SGML-ish (leaf tags often
unclosed), so parsing is regex-based rather than XML. Advantages over CSV: stable
FITID transaction ids (perfect dedupe) and a ledger balance (so these accounts get
real balances for net worth and balance_below rules).
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import Session

from ..ingest import IngestResult, TxnIn, ingest_transactions, upsert_account

_TXN_RE = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S | re.I)
_BAL_RE = re.compile(r"<LEDGERBAL>.*?<BALAMT>\s*(-?[\d.]+)", re.S | re.I)
_ORG_RE = re.compile(r"<ORG>\s*([^<\r\n]*)", re.I)


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def _parse_date(raw: str) -> date | None:
    digits = raw.strip()[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def parse_ofx(text: str) -> tuple[list[TxnIn], float | None, str]:
    """Returns (transactions, ledger balance or None, institution name)."""
    txns: list[TxnIn] = []
    for block in _TXN_RE.findall(text):
        posted = _parse_date(_tag(block, "DTPOSTED"))
        raw_amount = _tag(block, "TRNAMT")
        if posted is None or not raw_amount:
            continue
        try:
            amount = float(raw_amount)
        except ValueError:
            continue
        name = _tag(block, "NAME")
        memo = _tag(block, "MEMO")
        description = name if memo in ("", name) else f"{name} {memo}".strip()
        txns.append(
            TxnIn(
                posted=posted,
                amount=amount,
                description=description or "OFX transaction",
                external_id=_tag(block, "FITID") or None,
            )
        )
    if not txns:
        raise ValueError("No <STMTTRN> transactions found — is this an OFX/QFX file?")
    balance_match = _BAL_RE.search(text)
    balance = float(balance_match.group(1)) if balance_match else None
    org_match = _ORG_RE.search(text)
    institution = org_match.group(1).strip() if org_match else ""
    return txns, balance, institution


def import_ofx(
    session: Session,
    *,
    text: str,
    account_name: str,
    kind: str = "checking",
    owner: str = "joint",
) -> IngestResult:
    txns, balance, institution = parse_ofx(text)
    account = upsert_account(
        session,
        source="csv",
        name=account_name,
        kind=kind,
        owner=owner,
        institution=institution,
        balance=balance,
    )
    return ingest_transactions(session, account, txns)
