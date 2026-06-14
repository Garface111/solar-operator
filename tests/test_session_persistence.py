"""Per-login session persistence (June 2026).

THE BUG: token selection was "latest UtilitySession per (tenant, provider)", so
an operator who logged into client A's GMP, then logged out and into client B's
GMP, left A's accounts un-scrapeable — B's token clobbered A's as the only one
used. UtilitySession had no per-login identity.

THE FIX: sessions are keyed by the login's customer_number (GMP personId /
SmartHub custNbr), and pulls pick the token bound to each account's own
customer_number, falling back to latest-per-provider only when there's no
identity to match. Every client's login persists and stays independently usable.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, UtilitySession, now
from api.sessions import (
    token_for_account, session_for_account, session_customer_number,
)


def _tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="SessTest", contact_email=f"{tid}@t.test",
                      tenant_key=key, plan="standard", active=True))
        db.commit()
    return tid, key


# ─── unit: per-account token selection ──────────────────────────────────────

def test_token_selected_per_login_identity():
    """A1 belongs to login 111, B1 to login 222 captured LATER. A1 must still
    get login 111's token — not the latest (222) token."""
    tid, _ = _tenant()
    with SessionLocal() as db:
        db.add(UtilitySession(tenant_id=tid, provider="gmp", customer_number="111",
                              api_token="tokA", captured_at=now() - timedelta(hours=1)))
        db.add(UtilitySession(tenant_id=tid, provider="gmp", customer_number="222",
                              api_token="tokB", captured_at=now()))
        accA = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="A1", customer_number="111")
        accB = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="B1", customer_number="222")
        db.add(accA); db.add(accB); db.commit()

        assert token_for_account(db, accA) == "tokA"  # was "tokB" before the fix
        assert token_for_account(db, accB) == "tokB"


def test_legacy_fallback_when_no_identity_match():
    tid, _ = _tenant()
    with SessionLocal() as db:
        db.add(UtilitySession(tenant_id=tid, provider="gmp", customer_number=None,
                              api_token="old", captured_at=now() - timedelta(hours=2)))
        db.add(UtilitySession(tenant_id=tid, provider="gmp", customer_number="222",
                              api_token="latest", captured_at=now()))
        # No customer_number on the account → latest-per-provider.
        acc_null = UtilityAccount(tenant_id=tid, provider="gmp",
                                  account_number="N1", customer_number=None)
        # customer_number set but no session for it yet → still falls back.
        acc_unknown = UtilityAccount(tenant_id=tid, provider="gmp",
                                     account_number="U1", customer_number="999")
        db.add(acc_null); db.add(acc_unknown); db.commit()

        assert token_for_account(db, acc_null) == "latest"
        assert token_for_account(db, acc_unknown) == "latest"


def test_session_customer_number_helper():
    assert session_customer_number(
        [{"customer_number": "111"}, {"customer_number": "111"}]) == "111"
    # ambiguous (multiple distinct) → None, so caller stores unkeyed
    assert session_customer_number(
        [{"customer_number": "111"}, {"customer_number": "222"}]) is None
    assert session_customer_number([{"customer_number": None}]) is None
    assert session_customer_number([]) is None


# ─── integration: the real "log in as A, then B" capture flow ───────────────

def _gmp_payload(email: str, accounts: list[dict], token: str) -> dict:
    return {"provider": "gmp", "user": {"email": email, "username": email},
            "auth": {"apiToken": token}, "accounts": accounts}


def _acct(num: str, cust: str) -> dict:
    return {"accountNumber": num, "nickname": num, "customerNumber": cust,
            "serviceAddress": {"line1": num + " Main St"},
            "isPrimary": True, "solarNetMeter": True}


def _sync(client, key, payload):
    return client.post("/v1/sync", json=payload,
                       headers={"Authorization": f"Bearer {key}"})


def test_two_distinct_logins_both_persist_and_scrape(client):
    """Operator logs into client A (cust 111), logs out, logs into client B
    (cust 222). BOTH sessions persist; each account scrapes with its OWN
    login's token."""
    tid, key = _tenant()
    assert _sync(client, key, _gmp_payload(
        "a@gmp.test", [_acct("100", "111")], "tokA")).status_code == 200
    assert _sync(client, key, _gmp_payload(
        "b@gmp.test", [_acct("200", "222")], "tokB")).status_code == 200

    with SessionLocal() as db:
        sessions = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == tid)
        ).scalars().all()
        by_cust = {s.customer_number: s.api_token for s in sessions}
        # Client A's login was NOT clobbered by logging in as B.
        assert by_cust.get("111") == "tokA"
        assert by_cust.get("222") == "tokB"

        accA = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.account_number == "100")).scalar_one()
        accB = db.execute(select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.account_number == "200")).scalar_one()
        assert token_for_account(db, accA) == "tokA"
        assert token_for_account(db, accB) == "tokB"


def test_recapture_same_login_upserts_in_place(client):
    """Re-capturing the same login updates its token in place — one row per
    login identity, not an ever-growing pile of stale rows."""
    tid, key = _tenant()
    assert _sync(client, key, _gmp_payload(
        "a@gmp.test", [_acct("100", "111")], "tok1")).status_code == 200
    assert _sync(client, key, _gmp_payload(
        "a@gmp.test", [_acct("100", "111")], "tok2")).status_code == 200

    with SessionLocal() as db:
        sessions = db.execute(select(UtilitySession).where(
            UtilitySession.tenant_id == tid,
            UtilitySession.customer_number == "111")).scalars().all()
        assert len(sessions) == 1          # upserted, not appended
        assert sessions[0].api_token == "tok2"
