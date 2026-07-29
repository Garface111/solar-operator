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
    ("visa", "credit"),
    ("amex", "credit"),
    ("savings", "savings"),
    ("invest", "investment"),
    ("brokerage", "investment"),
    ("401", "investment"),
    ("ira", "investment"),
]

# Consulted only when the account name itself gives no hint — e.g. Fidelity
# names its brokerage accounts just "Individual (1234)".
_INSTITUTION_HINTS = [
    ("fidelity", "investment"),
    ("schwab", "investment"),
    ("vanguard", "investment"),
    ("invest", "investment"),
    ("brokerage", "investment"),
]


def claim_setup_token(setup_token: str) -> str:
    """Exchange a one-time SimpleFIN setup token for the permanent access URL."""
    claim_url = base64.b64decode(setup_token.strip()).decode()
    resp = httpx.post(claim_url, timeout=30)
    resp.raise_for_status()
    return resp.text.strip()


def _guess_kind(name: str, institution: str = "") -> str:
    lower = name.lower()
    for hint, kind in _KIND_HINTS:
        if hint in lower:
            return kind
    lower_inst = institution.lower()
    for hint, kind in _INSTITUTION_HINTS:
        if hint in lower_inst:
            return kind
    return "checking"


def fetch(access_url: str, lookback_days: int = 90) -> dict:
    start = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    resp = httpx.get(f"{access_url}/accounts", params={"start-date": start}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def sync(lookback_days: int = 90) -> dict:
    """Pull every configured SimpleFIN bridge into the local model.

    Each household member can hold their own bridge, so one spouse's failure
    (an expired connection, a bank outage) must not stop the other's from
    syncing — each is pulled independently and reported on its own.
    """
    urls = config.SIMPLEFIN_ACCESS_URLS
    if not urls:
        return {"status": "skipped", "detail": "no SimpleFIN access URL configured"}
    if len(urls) == 1:
        return _sync_one(urls[0], lookback_days)

    results = [_sync_one(url, lookback_days) for url in urls]
    failed = [r for r in results if r.get("status") != "ok"]
    return {
        "status": "ok" if not failed else ("partial" if len(failed) < len(results) else "error"),
        "bridges": len(results),
        "accounts": sum(r.get("accounts", 0) for r in results),
        "added": sum(r.get("added", 0) for r in results),
        "skipped": sum(r.get("skipped", 0) for r in results),
        "failures": [r.get("detail") for r in failed],
    }


def _sync_one(access_url: str, lookback_days: int = 90) -> dict:
    added = skipped = accounts_seen = 0
    try:
        data = fetch(access_url, lookback_days)
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
                    kind=_guess_kind(
                        acct.get("name") or "", (acct.get("org") or {}).get("name") or ""
                    ),
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
