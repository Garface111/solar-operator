"""
Tests for GMP auto-populate on the /v1/sync extension ingest path
(api/app.py). When a Client has gmp_autopopulate=True and its gmp_email
matches the captured GMP login, /v1/sync should create one Array (and link
one UtilityAccount) per captured account.

Covered:
  1. autopop=True + matching email → 3 arrays + 3 utility_accounts, linked
  2. autopop=False → no arrays (existing behavior: session + accounts only)
  3. autopop=True but gmp_email mismatch → no arrays
  4. idempotency → running the same sync twice yields 3 arrays, not 6

No network: /v1/sync only touches the DB on this path. Bearer auth uses the
tenant_key created inline per test (each test gets a fresh tenant so the
shared session-scoped sqlite DB stays conflict-free).
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, UtilitySession


# ─── helpers ───────────────────────────────────────────────────────────────

def _make_tenant_with_client(*, autopop: bool, gmp_email: str) -> tuple[str, str, int]:
    """Create a fresh tenant + one client. Returns (tenant_id, tenant_key, client_id)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Autopop Test Co", contact_email="op@autopop.test",
            tenant_key=key, plan="standard", active=True,
        ))
        c = Client(
            tenant_id=tid, name="Client " + tid, contact_email="client@autopop.test",
            gmp_email=gmp_email, gmp_autopopulate=autopop,
        )
        db.add(c)
        db.commit()
        return tid, key, c.id


def _account(account_number: str, nickname: str) -> dict:
    return {
        "accountNumber": account_number,
        "nickname": nickname,
        "customerNumber": "cust_" + account_number,
        "serviceAddress": {"line1": account_number + " Main St", "city": "Chester"},
        "isPrimary": True,
        "solarNetMeter": True,
    }


def _payload(email: str, accounts: list[dict]) -> dict:
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": "Captured User", "username": email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ─── (1) autopop + matching email creates arrays ─────────────────────────────

def test_autopop_creates_one_array_per_account(client):
    email = "match@gmp.test"
    tid, key, cid = _make_tenant_with_client(autopop=True, gmp_email=email)

    accounts = [_account("1001", "Roof"), _account("1002", "Barn"), _account("1003", "Field")]
    # Send a different-case email to also exercise case-insensitive matching.
    resp = _sync(client, key, _payload("Match@GMP.Test", accounts))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()

        assert len(arrays) == 3
        assert len(uaccts) == 3
        # all arrays belong to the matched client
        assert all(a.client_id == cid for a in arrays)
        # GMP default offset
        assert all(a.bill_offset_months == 1 for a in arrays)
        # every account linked to exactly one (distinct) array
        assert all(u.array_id is not None for u in uaccts)
        assert {u.array_id for u in uaccts} == {a.id for a in arrays}
        # array names came from nicknames
        assert {a.name for a in arrays} == {"Roof", "Barn", "Field"}

        c = db.get(Client, cid)
        assert c.gmp_last_sync_at is not None


# ─── (2) autopop off → existing behavior only ────────────────────────────────

def test_autopop_false_creates_no_arrays(client):
    email = "noauto@gmp.test"
    tid, key, cid = _make_tenant_with_client(autopop=False, gmp_email=email)

    resp = _sync(client, key, _payload(email, [_account("2001", "X"), _account("2002", "Y")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        assert db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all() == []

        # Existing behavior preserved: session persisted, accounts upserted (unlinked).
        sessions = db.execute(select(UtilitySession).where(UtilitySession.tenant_id == tid)).scalars().all()
        assert len(sessions) == 1
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        assert len(uaccts) == 2
        assert all(u.array_id is None for u in uaccts)

        c = db.get(Client, cid)
        assert c.gmp_last_sync_at is None


# ─── (3) autopop on but email mismatch → no arrays ───────────────────────────

def test_autopop_email_mismatch_creates_no_arrays(client):
    tid, key, cid = _make_tenant_with_client(autopop=True, gmp_email="client@gmp.test")

    resp = _sync(client, key, _payload("someone-else@gmp.test", [_account("3001", "Z")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        assert db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all() == []
        c = db.get(Client, cid)
        assert c.gmp_last_sync_at is None


# ─── (4) idempotency → second identical sync adds nothing ────────────────────

def test_autopop_is_idempotent(client):
    email = "idem@gmp.test"
    tid, key, cid = _make_tenant_with_client(autopop=True, gmp_email=email)

    accounts = [_account("4001", "A"), _account("4002", "B"), _account("4003", "C")]

    assert _sync(client, key, _payload(email, accounts)).status_code == 200
    # Re-capture (fresh auth token, same accounts) — must NOT duplicate arrays.
    assert _sync(client, key, _payload(email, accounts)).status_code == 200

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        assert len(arrays) == 3
        assert len(uaccts) == 3
