"""
Tests for the capture pipeline (bugs from operator meeting notes).

Covers:
  1. Three sequential captures with distinct GMP logins produce 3 clients + arrays
  2. A GMP capture of an account with no bill history still creates Array
  3. Re-capturing an existing GMP account returns result="updated", not "created"
  4. gmp_last_sync_at is updated on CREATED, UPDATED, and NOOP captures
  5. Live Capture response includes result + is_new_client fields
"""
from __future__ import annotations
import secrets
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount


def _tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Trifecta Test Co", contact_email=f"op_{tid}@test.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _account(number: str, nickname: str) -> dict:
    return {
        "accountNumber": number,
        "nickname": nickname,
        "customerNumber": "cust_" + number,
        "serviceAddress": {"line1": number + " Main St"},
        "isPrimary": True,
        "solarNetMeter": True,
    }


def _gmp_payload(email: str, accounts: list[dict]) -> dict:
    # Holder name derived from the email so each captured login gets a
    # distinct Client.name under the post-identity-master smart-name rules.
    # Pre-identity-master this could safely be a constant; the new naming
    # logic uses holder name as the top-priority signal and the unique
    # constraint on (tenant_id, client.name) trips when it's reused.
    local = email.split("@")[0]
    holder = local.replace(".", " ").replace("_", " ").title() + " (test)"
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": holder, "username": email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ── Test 1: 3 sequential distinct logins → 3 clients × N arrays ──────────────

def test_three_sequential_captures_create_three_clients(client):
    tid, key = _tenant()

    logins = [
        ("alpha@gmp.test", [_account("A001", "Alpha Roof")]),
        ("beta@gmp.test",  [_account("B001", "Beta Barn"), _account("B002", "Beta Field")]),
        ("gamma@gmp.test", [_account("G001", "Gamma South")]),
    ]
    for email, accounts in logins:
        r = _sync(client, key, _gmp_payload(email, accounts))
        assert r.status_code == 200, r.text

    with SessionLocal() as db:
        clients = db.execute(
            select(Client).where(Client.tenant_id == tid, Client.deleted_at.is_(None))
        ).scalars().all()
        arrays = db.execute(select(Array).where(Array.tenant_id == tid)).scalars().all()

    assert len(clients) == 3, f"expected 3 clients, got {len(clients)}: {[(c.id, c.name) for c in clients]}"
    assert len(arrays) == 4, f"expected 4 arrays (1+2+1), got {len(arrays)}"
    emails = {c.gmp_email for c in clients}
    assert emails == {"alpha@gmp.test", "beta@gmp.test", "gamma@gmp.test"}


# ── Test 2: non-generating account (no bill history) still creates Array ──────

def test_non_generating_account_creates_array(client):
    """A GMP capture payload for an account with solarNetMeter=False or no
    bills should still produce a UtilityAccount and Array row. The worker
    will simply store 0 bills for it initially; data fills in later."""
    tid, key = _tenant()

    # Account with solarNetMeter=False (e.g. a brand-new install not yet generating)
    acct = {
        "accountNumber": "NEW001",
        "nickname": "New Install",
        "customerNumber": "cust_NEW001",
        "serviceAddress": {"line1": "1 Green St"},
        "isPrimary": True,
        "solarNetMeter": False,   # ← key: NOT yet generating
    }
    payload = _gmp_payload("newinstall@gmp.test", [acct])
    r = _sync(client, key, payload)
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        ua = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tid,
                UtilityAccount.account_number == "NEW001",
            )
        ).scalar_one_or_none()
        assert ua is not None, "UtilityAccount not created for non-generating account"

        arr = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().first()
        assert arr is not None, "Array not created for non-generating account"
        assert arr.id == ua.array_id, "Array not linked to UtilityAccount"


# ── Test 3: re-capture returns result="updated", not "created" ───────────────

def test_recapture_returns_updated_result(client):
    """Second capture of the same GMP login should return result='updated'
    (not 'created') so the Live Capture banner stays quiet."""
    tid, key = _tenant()

    payload = _gmp_payload("repeat@gmp.test", [_account("R001", "Repeat Roof")])

    r1 = _sync(client, key, payload)
    assert r1.status_code == 200
    assert r1.json().get("result") == "created", f"first capture should be 'created', got: {r1.json()}"
    assert r1.json().get("is_new_client") is True

    # Re-capture: same email, same accounts
    r2 = _sync(client, key, _gmp_payload("repeat@gmp.test", [_account("R001", "Repeat Roof")]))
    assert r2.status_code == 200
    assert r2.json().get("result") == "updated", f"re-capture should be 'updated', got: {r2.json()}"
    assert r2.json().get("is_new_client") is False


# ── Test 4: gmp_last_sync_at updated on every capture type ───────────────────

def test_last_capture_at_updated_on_all_result_types(client):
    """gmp_last_sync_at must be set on CREATED, UPDATED, and the autopop=False path."""
    tid, key = _tenant()

    # ── CREATED case ─────────────────────────────────────────────────────
    r = _sync(client, key, _gmp_payload("created@gmp.test", [_account("C001", "Created")]))
    assert r.status_code == 200
    assert r.json().get("result") == "created"
    created_cid = r.json()["client"]["id"]
    with SessionLocal() as db:
        c = db.get(Client, created_cid)
        assert c.gmp_last_sync_at is not None, "gmp_last_sync_at not set on CREATED"

    # ── UPDATED case ──────────────────────────────────────────────────────
    r2 = _sync(client, key, _gmp_payload("created@gmp.test", [_account("C001", "Created")]))
    assert r2.json().get("result") == "updated"
    with SessionLocal() as db:
        c = db.get(Client, created_cid)
        assert c.gmp_last_sync_at is not None, "gmp_last_sync_at not set on UPDATED"

    # ── NOOP case (autopop=False client matches but opted out) ────────────
    tid2, key2 = _tenant()
    with SessionLocal() as db:
        db.add(Client(
            tenant_id=tid2, name="OptOut Client",
            gmp_email="optout@gmp.test",
            gmp_autopopulate=False,
        ))
        db.commit()
    r3 = _sync(client, key2, _gmp_payload("optout@gmp.test", [_account("OO1", "OptOut")]))
    assert r3.status_code == 200
    # autopop=False match → bumps last_sync_at even though no arrays are created
    with SessionLocal() as db:
        c = db.execute(
            select(Client).where(
                Client.tenant_id == tid2,
                Client.gmp_email == "optout@gmp.test",
            )
        ).scalar_one()
        assert c.gmp_last_sync_at is not None, "gmp_last_sync_at not set on autopop=False match"


# ── Test 5: /v1/sync response shape includes result + is_new_client ──────────

def test_sync_response_includes_result_fields(client):
    """The /v1/sync response must include result, is_new_client, and client."""
    tid, key = _tenant()
    r = _sync(client, key, _gmp_payload("shape@gmp.test", [_account("S001", "Shape")]))
    assert r.status_code == 200
    body = r.json()
    assert "result" in body, f"'result' missing from response: {body}"
    assert "is_new_client" in body, f"'is_new_client' missing from response: {body}"
    assert "client" in body, f"'client' missing from response: {body}"
    assert body["result"] in ("created", "updated", "noop")
    assert isinstance(body["is_new_client"], bool)
