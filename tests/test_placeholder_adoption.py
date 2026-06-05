"""
Tests for the placeholder-adoption branch on /v1/sync (Note4 — "no manual
entry of clients required"). When a tenant has a seed Client with
is_placeholder=True and a capture lands whose login does NOT match any
existing real Client, the placeholder is adopted:

  - renamed to the inferred customer name (account nickname for GMP,
    customer_name for VEC),
  - email + autopop flag backfilled so the NEXT capture from the same
    login flows through the normal autopop branch,
  - the captured accounts are attached as Arrays,
  - is_placeholder is cleared.

The seed placeholder is created by /v1/onboarding/signup; here we create
it inline so the test stands alone.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount


def _seed(*, with_placeholder: bool) -> tuple[str, str, int | None]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Placeholder Test Co", contact_email="op@ph.test",
            tenant_key=key, plan="standard", active=True,
        ))
        cid: int | None = None
        if with_placeholder:
            c = Client(
                tenant_id=tid,
                name="Your first client",
                contact_email=None,
                is_placeholder=True,
            )
            db.add(c)
            db.flush()
            cid = c.id
        db.commit()
        return tid, key, cid


def _gmp_payload(email: str, accounts: list[dict]) -> dict:
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": "Captured User", "username": email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _account(account_number: str, nickname: str) -> dict:
    return {
        "accountNumber": account_number,
        "nickname": nickname,
        "customerNumber": "cust_" + account_number,
        "serviceAddress": {"line1": account_number + " Main St", "city": "Chester"},
        "isPrimary": True,
        "solarNetMeter": True,
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ─── (1) placeholder gets adopted on first non-matching capture ────────────

def test_placeholder_adopted_on_first_capture(client):
    tid, key, cid = _seed(with_placeholder=True)
    email = "bruce@gmp.test"

    accounts = [_account("2001", "Spencer LLC"), _account("2002", "Spencer LLC Barn")]
    resp = _sync(client, key, _gmp_payload(email, accounts))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c is not None
        # Renamed using the captured nickname (GMP doesn't expose customer_name).
        assert c.name == "Spencer LLC", f"expected rename, got name={c.name!r}"
        assert c.gmp_email == email.lower(), c.gmp_email
        assert c.gmp_autopopulate is True
        assert c.is_placeholder is False
        # Both accounts attached as arrays.
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(arrays) == 2, [a.name for a in arrays]
        assert all(a.client_id == cid for a in arrays)
        uaccts = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        ).scalars().all()
        assert {u.account_number for u in uaccts} == {"2001", "2002"}
        assert all(u.array_id is not None for u in uaccts)


# ─── (2) second capture from same login flows through normal autopop ──────

def test_adopted_placeholder_handles_second_capture_via_autopop(client):
    tid, key, cid = _seed(with_placeholder=True)
    email = "bruce@gmp.test"

    # First capture: adopts the placeholder, creates 2 arrays.
    r1 = _sync(client, key, _gmp_payload(email, [_account("3001", "Solar A"), _account("3002", "Solar B")]))
    assert r1.status_code == 200, r1.text

    # Second capture with the SAME accounts — should be idempotent (no
    # duplicate arrays). This exercises the normal autopop branch since
    # the placeholder was backfilled with gmp_email + gmp_autopopulate.
    r2 = _sync(client, key, _gmp_payload(email, [_account("3001", "Solar A"), _account("3002", "Solar B")]))
    assert r2.status_code == 200, r2.text

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(arrays) == 2, [a.name for a in arrays]


# ─── (3) no placeholder + no match → auto-create new client (multi-login) ──

def test_no_placeholder_no_match_creates_no_arrays(client):
    """Under multi-login autopop, no placeholder + no match auto-CREATES a
    fresh Client + Arrays. (Pre-multi-login this was a no-op; renamed
    test kept for historical traceability.)"""
    tid, key, _ = _seed(with_placeholder=False)
    resp = _sync(client, key, _gmp_payload("nobody@gmp.test", [_account("4001", "X")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        clients = db.execute(select(Client).where(Client.tenant_id == tid)).scalars().all()
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(clients) == 1
        assert clients[0].gmp_email == "nobody@gmp.test"
        assert clients[0].gmp_autopopulate is True
        assert len(arrays) == 1
        assert arrays[0].client_id == clients[0].id


# ─── (4) real matching client wins — placeholder is left alone ────────────

def test_matching_real_client_beats_placeholder(client):
    """If both a placeholder AND a real autopop-enabled Client with a
    matching email exist, the real client owns the arrays and the
    placeholder is untouched."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    email = "real@gmp.test"
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Mixed Co", contact_email="op@mix.test",
            tenant_key=key, plan="standard", active=True,
        ))
        placeholder = Client(
            tenant_id=tid, name="Your first client", is_placeholder=True,
        )
        real = Client(
            tenant_id=tid, name="Real Client LLC",
            gmp_email=email, gmp_autopopulate=True,
        )
        db.add(placeholder)
        db.add(real)
        db.commit()
        ph_id = placeholder.id
        real_id = real.id

    resp = _sync(client, key, _gmp_payload(email, [_account("5001", "Roof")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        ph = db.get(Client, ph_id)
        assert ph.is_placeholder is True, "placeholder should be untouched"
        assert ph.name == "Your first client"
        assert ph.gmp_email is None
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        assert len(arrays) == 1
        assert arrays[0].client_id == real_id
