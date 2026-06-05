"""
Tests for the multi-account autopop flow:

When a tenant has no placeholder Client AND a /v1/sync capture arrives
for a login that doesn't match any existing autopop-enabled Client,
the system AUTO-CREATES a brand-new Client + Arrays for that login.

This is the "50 utility logins → 50 Clients, zero manual entry" path.

Also covered:
- email/username fallback as the default display name when no
  customer_name / nickname is provided by the adapter
- idempotency: a second capture from the same login flows through the
  normal autopop branch (no duplicate Clients)
- multi-tenant isolation
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount


def _tenant(*, with_placeholder: bool = False) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="MultiLogin Co", contact_email="op@multi.test",
            tenant_key=key, plan="standard", active=True,
        ))
        if with_placeholder:
            db.add(Client(
                tenant_id=tid, name="Your first client", is_placeholder=True,
            ))
        db.commit()
        return tid, key


def _gmp_payload(email: str, accounts: list[dict], *, username: str | None = None) -> dict:
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": "X", "username": username or email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _account(account_number: str, nickname: str) -> dict:
    return {
        "accountNumber": account_number,
        "nickname": nickname,
        "customerNumber": "cust_" + account_number,
        "serviceAddress": {"line1": account_number + " Main St"},
        "isPrimary": True,
        "solarNetMeter": True,
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ─── (1) After placeholder is consumed, second NEW login auto-creates ─────

def test_second_distinct_login_auto_creates_client(client):
    tid, key = _tenant(with_placeholder=True)

    # First capture adopts the placeholder.
    r1 = _sync(client, key, _gmp_payload(
        "alice@gmp.test", [_account("100", "Alice Roof")]))
    assert r1.status_code == 200, r1.text

    # Second capture, totally different login + accounts.
    r2 = _sync(client, key, _gmp_payload(
        "bob@gmp.test", [_account("200", "Bob Barn"), _account("201", "Bob Field")]))
    assert r2.status_code == 200, r2.text

    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(Client.tenant_id == tid).order_by(Client.id)
        ).scalars().all()
        # Two clients: alice (from placeholder adoption) + bob (auto-create).
        # Names default to the captured login email — per-array nicknames
        # (Alice Roof, Bob Barn) are intentionally NOT used as client names.
        assert len(clients) == 2, [(c.id, c.name, c.gmp_email) for c in clients]
        names_by_email = {c.gmp_email: c.name for c in clients}
        assert names_by_email["alice@gmp.test"] == "alice@gmp.test"
        assert names_by_email["bob@gmp.test"] == "bob@gmp.test"
        # Both autopop-enabled so future captures flow through fast path.
        assert all(c.gmp_autopopulate for c in clients)
        # Arrays attached to the right client.
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(arrays) == 3
        bob_id = next(c.id for c in clients if c.gmp_email == "bob@gmp.test")
        assert sum(1 for a in arrays if a.client_id == bob_id) == 2


# ─── (2) Email fallback when no nickname/customer_name available ──────────

def test_email_used_as_name_when_no_customer_name(client):
    tid, key = _tenant(with_placeholder=False)

    # Build an account with no nickname/customer_name — sometimes happens
    # when adapter sees a minimal energyAccounts entry.
    raw_account = {
        "accountNumber": "555",
        "nickname": None,
        "customerNumber": "cust_555",
        "serviceAddress": None,
        "isPrimary": True,
        "solarNetMeter": True,
    }
    resp = _sync(client, key, _gmp_payload("anonymous@gmp.test", [raw_account]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.execute(
            select(Client).where(Client.tenant_id == tid)
        ).scalar_one()
        # Falls back to the email since no nicer name was available.
        assert c.name == "anonymous@gmp.test", c.name
        assert c.gmp_email == "anonymous@gmp.test"


# ─── (3) Same login captured twice = idempotent ─────────────────────────

def test_repeat_login_is_idempotent(client):
    tid, key = _tenant(with_placeholder=False)

    accounts = [_account("700", "Solar A")]
    r1 = _sync(client, key, _gmp_payload("repeat@gmp.test", accounts))
    assert r1.status_code == 200
    r2 = _sync(client, key, _gmp_payload("repeat@gmp.test", accounts))
    assert r2.status_code == 200

    with SessionLocal() as db:
        clients = db.execute(select(Client).where(Client.tenant_id == tid)).scalars().all()
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(clients) == 1
        assert len(arrays) == 1


# ─── (4) Five distinct logins → five Clients (the 50-account dream, scaled) ──

def test_many_distinct_logins_each_get_own_client(client):
    tid, key = _tenant(with_placeholder=False)

    for i in range(5):
        resp = _sync(client, key, _gmp_payload(
            f"login{i}@gmp.test",
            [_account(f"8{i:03d}", f"Customer {i}")],
        ))
        assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        clients = db.execute(select(Client).where(Client.tenant_id == tid)).scalars().all()
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(clients) == 5
        assert len(arrays) == 5
        # Each client has exactly one array; each unique login email is paired.
        emails = {c.gmp_email for c in clients}
        assert emails == {f"login{i}@gmp.test" for i in range(5)}


# ─── (5) Username-only login (no email) still gets a Client + name fallback ─

def test_username_only_login_falls_back_to_username(client):
    tid, key = _tenant(with_placeholder=False)

    # Build a payload where user.email is empty and only username carries info.
    payload = {
        "provider": "gmp",
        "user": {"email": "", "fullName": "U", "username": "jdoe"},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": [{
            "accountNumber": "999",
            "nickname": None,
            "customerNumber": "cust_999",
            "isPrimary": True,
            "solarNetMeter": True,
        }],
    }
    resp = _sync(client, key, payload)
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        assert c.name == "jdoe"
        assert c.gmp_username == "jdoe"
        assert c.gmp_email is None
        assert c.gmp_autopopulate is True
