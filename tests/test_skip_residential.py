"""
Tests for the residential-account filter on the /v1/sync path.

GMP accounts with solarNetMeter=True (or groupNetMetered=True) are
generation accounts and get the full Client→Array treatment. Accounts
with neither flag set are residential: they land as UtilityAccount rows
with is_residential=True but do NOT trigger Client or Array creation.

VEC and WEC pass-through: no residential flag in those payloads yet, so
classify_residential always returns False for them.

Covered:
  Unit (classify_residential):
    1. GMP solar_net_meter=True → False (is generation)
    2. GMP both flags False → True (is residential)
    3. GMP no flags in extra → True (missing = residential default)
    4. VEC → False (no signal, pass-through)
    5. WEC → False (no adapter yet / no signal)
    6. Provider casing: "gmp" same as "GMP"

  Integration (/v1/sync):
    7. Mixed GMP capture: generation accounts get Arrays; residential
       accounts get UtilityAccount rows with is_residential=True and NO
       Array or Client created for them alone.
    8. All-residential capture: no Client created at all.
    9. Idempotency: re-capturing the same residential account does NOT
       trigger Client creation on the second call.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount
from api.sync_filter import classify_residential


# ─── helpers ────────────────────────────────────────────────────────────────

def _tenant() -> tuple[str, str]:
    """Create a minimal tenant. Returns (tenant_id, tenant_key)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Residential Test Co",
            contact_email="op@residential.test",
            tenant_key=key, plan="standard", active=True,
        ))
        db.commit()
    return tid, key


def _generation_account(acct_no: str, nickname: str = "") -> dict:
    """Raw GMP account dict with solarNetMeter=True (generation)."""
    return {
        "accountNumber": acct_no,
        "nickname": nickname or acct_no,
        "customerNumber": "cust_" + acct_no,
        "solarNetMeter": True,
        "groupNetMetered": False,
        "isPrimary": True,
        "serviceAddress": {"line1": "1 Solar St", "city": "Burlington"},
    }


def _residential_account(acct_no: str, nickname: str = "") -> dict:
    """Raw GMP account dict with solarNetMeter=False (residential)."""
    return {
        "accountNumber": acct_no,
        "nickname": nickname or acct_no,
        "customerNumber": "cust_" + acct_no,
        "solarNetMeter": False,
        "groupNetMetered": False,
        "isPrimary": False,
        "serviceAddress": {"line1": "42 Maple Ave", "city": "Montpelier"},
    }


def _gmp_payload(email: str, accounts: list[dict]) -> dict:
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": "Test Operator", "username": email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _sync(client, key: str, payload: dict):
    return client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})


# ─── unit tests: classify_residential ────────────────────────────────────────

def test_classify_residential_gmp_solar_net_meter_true():
    assert classify_residential("GMP", {"extra": {"solarNetMeter": True}}) is False


def test_classify_residential_gmp_both_flags_false():
    assert classify_residential("GMP", {"extra": {"solarNetMeter": False, "groupNetMetered": False}}) is True


def test_classify_residential_gmp_no_flags():
    assert classify_residential("GMP", {"extra": {}}) is True


def test_classify_residential_vec_passthrough():
    assert classify_residential("VEC", {"extra": {}}) is False


def test_classify_residential_wec_passthrough():
    assert classify_residential("WEC", {"extra": {}}) is False


def test_classify_residential_provider_casing():
    # lowercase "gmp" should behave identically to "GMP"
    assert classify_residential("gmp", {"extra": {}}) is True
    assert classify_residential("gmp", {"extra": {"solarNetMeter": True}}) is False


# ─── integration tests ───────────────────────────────────────────────────────

def test_mixed_capture_generation_gets_array_residential_does_not(client):
    """5 generation + 3 residential → 5 Arrays, 3 residential UtilityAccounts,
    one Client (auto-created for the generation login)."""
    tid, key = _tenant()
    email = "mixed@gmp.test"

    gen_accounts = [_generation_account(f"GEN{i}", f"Solar {i}") for i in range(5)]
    res_accounts = [_residential_account(f"RES{i}", f"Home {i}") for i in range(3)]

    resp = _sync(client, key, _gmp_payload(email, gen_accounts + res_accounts))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["residential_count"] == 3

    with SessionLocal() as db:
        all_accounts = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        ).scalars().all()
        all_arrays = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().all()
        all_clients = db.execute(
            select(Client).where(
                Client.tenant_id == tid,
                Client.deleted_at.is_(None),
            )
        ).scalars().all()

        assert len(all_accounts) == 8, "All 8 accounts should be persisted"

        gen_uaccts = [a for a in all_accounts if not a.is_residential]
        res_uaccts = [a for a in all_accounts if a.is_residential]
        assert len(gen_uaccts) == 5, "5 generation accounts"
        assert len(res_uaccts) == 3, "3 residential accounts"

        # Generation accounts all have Arrays
        for ua in gen_uaccts:
            assert ua.array_id is not None, f"Generation account {ua.account_number} missing array"

        # Residential accounts have NO arrays
        for ua in res_uaccts:
            assert ua.array_id is None, f"Residential account {ua.account_number} should not have array"

        assert len(all_arrays) == 5, "5 arrays (one per generation account)"
        assert len(all_clients) == 1, "One Client auto-created for the generation login"


def test_all_residential_capture_no_client_created(client):
    """A login with only residential accounts → no Client or Array created."""
    tid, key = _tenant()
    email = "allres@gmp.test"

    res_accounts = [_residential_account(f"R{i}") for i in range(4)]

    resp = _sync(client, key, _gmp_payload(email, res_accounts))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["residential_count"] == 4
    assert data["result"] == "noop"

    with SessionLocal() as db:
        all_accounts = db.execute(
            select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        ).scalars().all()
        all_arrays = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().all()
        all_clients = db.execute(
            select(Client).where(
                Client.tenant_id == tid,
                Client.deleted_at.is_(None),
            )
        ).scalars().all()

        assert len(all_accounts) == 4, "4 residential UtilityAccount rows persisted"
        assert all(a.is_residential for a in all_accounts), "All marked is_residential=True"
        assert len(all_arrays) == 0, "No arrays created"
        assert len(all_clients) == 0, "No client created"


def test_residential_recapture_idempotent(client):
    """Re-capturing the same residential accounts a second time does NOT
    create a Client or Array on the second call."""
    tid, key = _tenant()
    email = "idempotent@gmp.test"
    res_accounts = [_residential_account("IDEM1"), _residential_account("IDEM2")]
    payload = _gmp_payload(email, res_accounts)

    resp1 = _sync(client, key, payload)
    assert resp1.status_code == 200

    resp2 = _sync(client, key, payload)
    assert resp2.status_code == 200
    assert resp2.json()["residential_count"] == 2

    with SessionLocal() as db:
        all_arrays = db.execute(
            select(Array).where(Array.tenant_id == tid)
        ).scalars().all()
        all_clients = db.execute(
            select(Client).where(
                Client.tenant_id == tid,
                Client.deleted_at.is_(None),
            )
        ).scalars().all()

        assert len(all_arrays) == 0, "Still no arrays after second capture"
        assert len(all_clients) == 0, "Still no client after second capture"
