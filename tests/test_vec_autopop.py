"""
Tests for VEC auto-populate on the /v1/sync extension ingest path.

When a Client has vec_autopopulate=True and its vec_email/vec_username matches
the captured VEC login, /v1/sync should create one Array (and link one
UtilityAccount) per captured account with bill_offset_months=0 (same-month).

Mirrors test_autopop.py (GMP coverage) for VEC. Covered:
  1. autopop=True + matching email → arrays created with bill_offset_months=0
  2. autopop=False → no arrays
  3. email mismatch → no arrays
  4. idempotency → second identical sync adds nothing
  5. matching username (email differs) → arrays created
  6. username-keyed client doesn't match on a stray email
  7. GMP sync doesn't trigger VEC autopop (providers are isolated)
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, UtilitySession


# ─── helpers ───────────────────────────────────────────────────────────────

def _make_tenant_with_client(
    *, vec_autopop: bool, vec_email: str | None = None, vec_username: str | None = None,
    gmp_autopop: bool = False, gmp_email: str | None = None,
) -> tuple[str, str, int]:
    """Create a fresh tenant + one client. Returns (tenant_id, tenant_key, client_id)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="VEC Autopop Test Co", contact_email="op@vec-autopop.test",
            tenant_key=key, plan="standard", active=True,
        ))
        c = Client(
            tenant_id=tid, name="Client " + tid, contact_email="client@vec-autopop.test",
            vec_email=vec_email, vec_username=vec_username, vec_autopopulate=vec_autopop,
            gmp_email=gmp_email, gmp_autopopulate=gmp_autopop,
        )
        db.add(c)
        db.commit()
        return tid, key, c.id


def _vec_account(account_number: str, nickname: str) -> dict:
    return {
        "accountNumber": account_number,
        "customerName": nickname,  # VEC adapter reads customerName → normalized nickname
        "customerNumber": "vec_cust_" + account_number,
        "serviceAddress": {"line1": account_number + " Maple St", "city": "Burlington"},
    }


def _vec_payload(email: str, accounts: list[dict], username: str | None = None) -> dict:
    return {
        "provider": "vec",
        "user": {
            "email": email,
            "fullName": "VEC User",
            "username": username if username is not None else email,
        },
        "auth": {"apiToken": "vec_jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
        "bills_raw": [],
        "usage_raw": [],
    }


def _gmp_payload(email: str, accounts: list[dict]) -> dict:
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": "GMP User", "username": email},
        "auth": {"apiToken": "gmp_jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _gmp_account(account_number: str, nickname: str) -> dict:
    return {
        "accountNumber": account_number,
        "nickname": nickname,
        "customerNumber": "gmp_cust_" + account_number,
        "serviceAddress": {"line1": account_number + " Main St", "city": "Chester"},
        "isPrimary": True,
        "solarNetMeter": True,
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ─── (1) VEC autopop + matching email creates arrays ─────────────────────────

def test_vec_autopop_creates_one_array_per_account(client):
    email = "vec-match@vec.test"
    tid, key, cid = _make_tenant_with_client(vec_autopop=True, vec_email=email)

    accounts = [_vec_account("V001", "North"), _vec_account("V002", "South")]
    resp = _sync(client, key, _vec_payload("Vec-Match@VEC.Test", accounts))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()

        assert len(arrays) == 2
        assert len(uaccts) == 2
        assert all(a.client_id == cid for a in arrays)
        # VEC uses same-month billing (bill_offset_months=0)
        assert all(a.bill_offset_months == 0 for a in arrays)
        assert all(u.array_id is not None for u in uaccts)
        assert {u.array_id for u in uaccts} == {a.id for a in arrays}
        assert {a.name for a in arrays} == {"North", "South"}

        c = db.get(Client, cid)
        assert c.vec_last_sync_at is not None


# ─── (2) VEC autopop off → no arrays ─────────────────────────────────────────

def test_vec_autopop_false_creates_no_arrays(client):
    email = "vec-noauto@vec.test"
    tid, key, cid = _make_tenant_with_client(vec_autopop=False, vec_email=email)

    resp = _sync(client, key, _vec_payload(email, [_vec_account("V101", "X")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        assert db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all() == []

        sessions = db.execute(select(UtilitySession).where(UtilitySession.tenant_id == tid)).scalars().all()
        assert len(sessions) == 1
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        assert len(uaccts) == 1
        assert all(u.array_id is None for u in uaccts)

        c = db.get(Client, cid)
        # Under multi-login autopop, the matching-email-but-autopop-False client
        # still gets its sync timestamp bumped so the operator knows the capture
        # reached us. Arrays are NOT imported — autopop=False is respected.
        assert c.vec_last_sync_at is not None
        # No auto-created client either — existing match (even autopop=False)
        # blocks the auto-create branch.
        assert len(db.execute(select(Client).where(Client.tenant_id == tid)).scalars().all()) == 1


# ─── (3) VEC autopop on but email mismatch → auto-create new client ──────────

def test_vec_autopop_email_mismatch_creates_no_arrays(client):
    """Under multi-login autopop, an unmatched VEC capture auto-creates a
    brand-new Client + Arrays. The original Client is left alone."""
    tid, key, cid = _make_tenant_with_client(vec_autopop=True, vec_email="real-client@vec.test")

    resp = _sync(client, key, _vec_payload("different@vec.test", [_vec_account("V201", "Z")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        orig = db.get(Client, cid)
        # Original untouched.
        assert orig.vec_last_sync_at is None
        orig_arrays = db.execute(
            select(Array).where(Array.client_id == cid)
        ).scalars().all()
        assert orig_arrays == []
        # New auto-created Client owns the captured array.
        all_clients = db.execute(
            select(Client).where(Client.tenant_id == tid).order_by(Client.id)
        ).scalars().all()
        assert len(all_clients) == 2
        new_c = all_clients[1]
        assert new_c.vec_email == "different@vec.test"
        assert new_c.vec_autopopulate is True


# ─── (4) VEC idempotency → second identical sync adds nothing ─────────────────

def test_vec_autopop_is_idempotent(client):
    email = "vec-idem@vec.test"
    tid, key, cid = _make_tenant_with_client(vec_autopop=True, vec_email=email)

    accounts = [_vec_account("V301", "A"), _vec_account("V302", "B")]

    assert _sync(client, key, _vec_payload(email, accounts)).status_code == 200
    assert _sync(client, key, _vec_payload(email, accounts)).status_code == 200

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        assert len(arrays) == 2
        assert len(uaccts) == 2


# ─── (5) VEC autopop matches on username when the email differs ───────────────

def test_vec_autopop_matches_on_username(client):
    tid, key, cid = _make_tenant_with_client(vec_autopop=True, vec_username="vecuser1")

    accounts = [_vec_account("V401", "East"), _vec_account("V402", "West")]
    resp = _sync(client, key, _vec_payload(
        "unrelated@vec.test", accounts, username="VecUser1"))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()
        uaccts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        assert len(arrays) == 2
        assert all(a.client_id == cid for a in arrays)
        assert {u.array_id for u in uaccts} == {a.id for a in arrays}
        assert {a.name for a in arrays} == {"East", "West"}
        c = db.get(Client, cid)
        assert c.vec_last_sync_at is not None


# ─── (6) VEC username-keyed client doesn't match a stray email ───────────────

def test_vec_autopop_username_client_ignores_email(client):
    """Username-keyed Client doesn't cross-match on email. Under multi-login
    autopop, the unmatched capture spawns a fresh Client instead."""
    tid, key, cid = _make_tenant_with_client(vec_autopop=True, vec_username="vecuser2")

    resp = _sync(client, key, _vec_payload(
        "vecuser2@vec.test", [_vec_account("V501", "Q")], username="someone-else"))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        orig = db.get(Client, cid)
        assert orig.vec_last_sync_at is None
        orig_arrays = db.execute(select(Array).where(Array.client_id == cid)).scalars().all()
        assert orig_arrays == []
        # New Client auto-created for the unmatched login.
        all_clients = db.execute(
            select(Client).where(Client.tenant_id == tid).order_by(Client.id)
        ).scalars().all()
        assert len(all_clients) == 2
        new_c = all_clients[1]
        assert new_c.vec_email == "vecuser2@vec.test"
        assert new_c.vec_username == "someone-else"


# ─── (7) GMP capture doesn't trigger VEC autopop ─────────────────────────────

def test_gmp_capture_does_not_trigger_vec_autopop(client):
    # Client has vec_autopopulate=True with vec_email matching the GMP capture email.
    # A GMP sync must NOT trigger VEC autopop — providers are isolated.
    # Under multi-login autopop, the GMP capture with no matching GMP client
    # will auto-create a NEW gmp-keyed Client; the VEC client is untouched.
    email = "shared@test.test"
    tid, key, cid = _make_tenant_with_client(vec_autopop=True, vec_email=email)

    resp = _sync(client, key, _gmp_payload(email, [_gmp_account("G601", "Farm")]))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        c = db.get(Client, cid)
        # The VEC client is unchanged — provider isolation holds.
        assert c.vec_last_sync_at is None
        assert c.gmp_email is None
        # The auto-created Client lives separately under gmp_email.
        all_clients = db.execute(
            select(Client).where(Client.tenant_id == tid).order_by(Client.id)
        ).scalars().all()
        assert len(all_clients) == 2
        new_c = all_clients[1]
        assert new_c.gmp_email == email
        assert new_c.vec_email is None
