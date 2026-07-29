"""SimpleFIN Bridge connector — read-only by protocol.

Flow: the user connects banks at beta-bridge.simplefin.org, generates a one-time
setup token, and we claim it once for a permanent read-only access URL. That URL is
the only credential this app ever holds; it cannot move money.

CLI: python -m bankai.connectors.simplefin claim <SETUP_TOKEN>
"""
from __future__ import annotations

import base64
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

from .. import config
from ..db import session_scope
from ..ingest import TxnIn, ingest_transactions, upsert_account
from ..models import SyncLog

_KIND_HINTS = [
    ("credit", "credit"),
    ("card", "credit"),
    ("savings", "savings"),
    ("invest", "investment"),
    ("brokerage", "investment"),
    ("401", "investment"),
    ("ira", "investment"),
]


def claim_setup_token(setup_token: str) -> str:
    """Exchange a one-time SimpleFIN setup token for the permanent access URL."""
    claim_url = base64.b64decode(setup_token.strip()).decode()
    resp = httpx.post(claim_url, timeout=30)
    resp.raise_for_status()
    return resp.text.strip()


def _guess_kind(name: str) -> str:
    lower = name.lower()
    for hint, kind in _KIND_HINTS:
        if hint in lower:
            return kind
    return "checking"


def fetch(access_url: str, lookback_days: int = 90) -> dict:
    start = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    resp = httpx.get(f"{access_url}/accounts", params={"start-date": start}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def sync(lookback_days: int = 90) -> dict:
    """Pull accounts + transactions from SimpleFIN into the local model."""
    if not config.SIMPLEFIN_ACCESS_URL:
        return {"status": "skipped", "detail": "SIMPLEFIN_ACCESS_URL not configured"}
    added = skipped = accounts_seen = 0
    try:
        data = fetch(config.SIMPLEFIN_ACCESS_URL, lookback_days)
        with session_scope() as session:
            for acct in data.get("accounts", []):
                accounts_seen += 1
                balance_date = acct.get("balance-date")
                account = upsert_account(
                    session,
                    source="simplefin",
                    external_id=acct.get("id"),
                    name=acct.get("name") or "Unnamed account",
                    institution=(acct.get("org") or {}).get("name", ""),
                    kind=_guess_kind(acct.get("name") or ""),
                    currency=acct.get("currency") or "USD",
                    balance=float(acct.get("balance") or 0),
                    balance_date=(
                        datetime.fromtimestamp(balance_date, tz=timezone.utc).replace(tzinfo=None)
                        if balance_date
                        else None
                    ),
                )
                txns = [
                    TxnIn(
                        posted=date.fromtimestamp(t.get("posted") or t.get("transacted_at") or 0),
                        amount=float(t.get("amount") or 0),
                        description=t.get("description") or "",
                        external_id=t.get("id"),
                        pending=bool(t.get("pending")),
                    )
                    for t in acct.get("transactions", [])
                    if t.get("posted") or t.get("transacted_at")
                ]
                result = ingest_transactions(session, account, txns)
                added += result.added
                skipped += result.skipped
            session.add(
                SyncLog(
                    source="simplefin",
                    status="ok",
                    detail=f"accounts={accounts_seen} added={added} skipped={skipped}",
                )
            )
        return {"status": "ok", "accounts": accounts_seen, "added": added, "skipped": skipped}
    except Exception as exc:  # log the failure, never crash the scheduler
        with session_scope() as session:
            session.add(SyncLog(source="simplefin", status="error", detail=str(exc)[:2000]))
        return {"status": "error", "detail": str(exc)}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "claim":
        url = claim_setup_token(sys.argv[2])
        print("Access URL (put this in .env as SIMPLEFIN_ACCESS_URL):")
        print(url)
    elif len(sys.argv) == 2 and sys.argv[1] == "sync":
        from ..db import init_db

        init_db()
        print(sync())
    else:
        print("usage: python -m bankai.connectors.simplefin claim <SETUP_TOKEN> | sync")
