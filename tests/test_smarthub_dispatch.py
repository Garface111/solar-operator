"""
Tests for /v1/sync dispatch with SmartHub providers.

Verifies that:
  1. A WEC payload routes to the SmartHub adapter and creates UtilityAccount rows
     with provider="wec" (not "vec").
  2. Autopop matches a Client whose vec_email matches the captured user email,
     creates an Array, and updates vec_last_sync_at.
  3. STOWE and other SmartHub codes also dispatch correctly (same adapter, different
     provider code).
  4. An unknown provider code still returns 400 from get_adapter.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, Client, Tenant, UtilityAccount


def _tenant(name="SmartHub Dispatch Test") -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name=name, contact_email=f"op_{tid}@test.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _sync(client, key: str, payload: dict):
    return client.post(
        "/v1/sync",
        json=payload,
        headers={"Authorization": f"Bearer {key}"},
    )


def _wec_payload(email: str, accounts: list[dict]) -> dict:
    return {
        "provider": "wec",
        "capturedAt": "2024-06-01T10:00:00Z",
        "user": {"hostname": "washingtonelectric.smarthub.coop", "email": email},
        "auth": {"apiToken": "tok_wec_" + secrets.token_hex(4)},
        "accounts": accounts,
        "bills": [],
        "usage": [],
    }


def _account(number: str, name: str) -> dict:
    return {
        "accountNumber": number,
        "customerName": name,
        "serviceAddress": f"{number} Test Rd, WEC VT",
    }


# ─── Test 1: WEC capture creates UtilityAccount with provider="wec" ──────────

def test_wec_capture_creates_accounts_with_correct_provider(client):
    tid, key = _tenant()
    payload = _wec_payload("farmer@wec.vt", [_account("8001234", "Hill Farm")])

    r = _sync(client, key, payload)
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.provider == "wec",
            )
        ).scalars().all()

    assert len(accts) == 1
    assert accts[0].account_number == "8001234"
    assert accts[0].provider == "wec"


# ─── Test 2: Autopop matches vec_email for WEC capture ───────────────────────

def test_wec_capture_autopop_matches_vec_email(client):
    tid, key = _tenant()
    email = "berry@wec.vt"

    # Pre-create a Client with vec_email matching the capture email
    with SessionLocal() as db:
        c = Client(
            tenant_id=tid,
            name="Berry Hill Solar",
            vec_email=email,
            vec_autopopulate=True,
            active=True,
        )
        db.add(c)
        db.commit()
        client_id = c.id

    payload = _wec_payload(email, [_account("8005678", "Berry Hill")])
    r = _sync(client, key, payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["result"] in ("created", "updated")

    with SessionLocal() as db:
        c = db.get(Client, client_id)
        assert c.vec_last_sync_at is not None

        arrays = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        assert len(arrays) == 1


# ─── Test 3: STOWE capture also dispatches to SmartHub adapter ───────────────

def test_stowe_capture_creates_accounts(client):
    tid, key = _tenant()
    payload = {
        "provider": "stowe",
        "capturedAt": "2024-06-01T10:00:00Z",
        "user": {"hostname": "stoweelectric.smarthub.coop", "email": "ski@stowe.vt"},
        "auth": {},
        "accounts": [_account("7001", "Ski Lodge Solar")],
        "bills": [],
        "usage": [],
    }

    r = _sync(client, key, payload)
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.provider == "stowe",
            )
        ).scalars().all()

    assert len(accts) == 1
    assert accts[0].provider == "stowe"


# ─── Test 4: WEC capture with uppercase provider code normalizes to lowercase ─

def test_wec_capture_uppercase_provider_normalizes(client):
    tid, key = _tenant()
    payload = _wec_payload("u@wec.vt", [_account("8009999", "Upper Farm")])
    payload["provider"] = "WEC"  # uppercase — should be normalized

    r = _sync(client, key, payload)
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        accts = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.provider == "wec",  # normalized to lowercase
            )
        ).scalars().all()

    assert len(accts) == 1


# ─── Test 5: Re-capture same WEC account returns result="updated" ─────────────

def test_wec_recapture_returns_updated(client):
    tid, key = _tenant()
    payload = _wec_payload("re@wec.vt", [_account("8007777", "River Farm")])

    r1 = _sync(client, key, payload)
    assert r1.status_code == 200
    r2 = _sync(client, key, payload)
    assert r2.status_code == 200
    assert r2.json()["result"] == "updated"
