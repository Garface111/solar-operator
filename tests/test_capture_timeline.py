"""
Tests for the capture timeline feature.

Covers:
  1. GET /v1/dev/captures returns 403 when SO_DEV_ENABLED is off.
  2. With SO_DEV_ENABLED=1, a GMP sync produces CaptureEvent rows and the
     endpoint returns them grouped by capture_id.
  3. Tenant isolation: tenant A's captures are not visible to tenant B.
  4. /v1/dev/captures/{capture_id} returns 404 for an unknown capture_id.
  5. Expected stage sequence after a new-client GMP sync.
  6. Privacy: auth.apiToken is never stored in payload_excerpt.
"""
from __future__ import annotations

import json
import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, CaptureEvent
from api.account import mint_session_for_tenant


# ─── helpers ───────────────────────────────────────────────────────────────

def _tenant(suffix: str = "") -> tuple[str, str]:
    """Create a minimal tenant. Returns (tenant_id, tenant_key)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name=f"Timeline Test {suffix}",
            contact_email=f"op_{tid}@test.test",
            tenant_key=key,
            plan="standard",
            active=True,
        ))
        db.commit()
    return tid, key


def _auth(tid: str) -> dict:
    """Dashboard session header for the given tenant_id."""
    return {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}


def _bearer(key: str) -> dict:
    """Extension bearer header (tenant_key) for /v1/sync."""
    return {"Authorization": f"Bearer {key}"}


def _gmp_payload(email: str, accounts: list[dict]) -> dict:
    local = email.split("@")[0]
    holder = local.replace(".", " ").title() + " (tl)"
    return {
        "provider": "gmp",
        "user": {"email": email, "fullName": holder, "username": email},
        "auth": {"apiToken": "jwt_" + secrets.token_hex(6)},
        "accounts": accounts,
    }


def _account(number: str, nickname: str) -> dict:
    return {
        "accountNumber": number,
        "nickname": nickname,
        "customerNumber": "cust_" + number,
        "serviceAddress": {"line1": f"{number} Main St"},
        "isPrimary": True,
        "solarNetMeter": True,
    }


# ─── test: DEV_ENABLED=off → 403 ───────────────────────────────────────────

def test_captures_disabled_returns_403(client, monkeypatch):
    """When SO_DEV_ENABLED is off, /v1/dev/captures must refuse with 403."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", False)

    tid, _key = _tenant("dis")
    r = client.get("/v1/dev/captures", headers=_auth(tid))
    assert r.status_code == 403, r.text


# ─── test: enabled → rows returned after sync ──────────────────────────────

def test_captures_returns_rows_after_sync(client, monkeypatch):
    """A GMP sync should produce CaptureEvent rows visible on the endpoint."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", True)

    tid, key = _tenant("ena")
    payload = _gmp_payload(
        "ena_user@example.test",
        [_account("1111-2222", "North Field"), _account("3333-4444", "South Pasture")],
    )

    r = client.post("/v1/sync", json=payload, headers=_bearer(key))
    assert r.status_code == 200, r.text

    r2 = client.get("/v1/dev/captures", headers=_auth(tid))
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True
    assert len(body["captures"]) >= 1

    capture = body["captures"][0]
    assert "capture_id" in capture
    assert "started_at" in capture
    # At minimum: ingest_received + client_created + array_created x2
    assert len(capture["events"]) >= 2


# ─── test: expected stage sequence ─────────────────────────────────────────

def test_capture_stage_sequence(client, monkeypatch):
    """Expected stages for a new-client GMP sync: ingest_received, client_created, array_created×N."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", True)

    tid, key = _tenant("seq")
    payload = _gmp_payload(
        "seq_user@example.test",
        [_account("5555-6666", "East Wing"), _account("7777-8888", "West Lot")],
    )
    r = client.post("/v1/sync", json=payload, headers=_bearer(key))
    assert r.status_code == 200

    with SessionLocal() as db:
        events = db.execute(
            select(CaptureEvent)
            .where(CaptureEvent.tenant_id == tid)
            .order_by(CaptureEvent.created_at)
        ).scalars().all()

    stages = [e.stage for e in events]
    assert "ingest_received" in stages
    assert "client_created" in stages
    assert stages.count("array_created") == 2


# ─── test: tenant isolation ─────────────────────────────────────────────────

def test_tenant_isolation(client, monkeypatch):
    """Tenant A's captures must not be visible to tenant B."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", True)

    tid_a, key_a = _tenant("iso_a")
    tid_b, _key_b = _tenant("iso_b")

    payload = _gmp_payload(
        "iso_a@example.test",
        [_account("9999-0000", "Isolated Array")],
    )
    r = client.post("/v1/sync", json=payload, headers=_bearer(key_a))
    assert r.status_code == 200

    r2 = client.get("/v1/dev/captures", headers=_auth(tid_b))
    assert r2.status_code == 200
    assert r2.json()["captures"] == []


# ─── test: 404 for unknown capture_id ─────────────────────────────────────

def test_get_capture_not_found(client, monkeypatch):
    """GET /v1/dev/captures/{id} with a nonexistent capture_id returns 404."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", True)

    tid, _key = _tenant("nf")
    r = client.get(
        "/v1/dev/captures/00000000-0000-0000-0000-000000000000",
        headers=_auth(tid),
    )
    assert r.status_code == 404


# ─── test: fetch by capture_id ─────────────────────────────────────────────

def test_get_capture_by_id(client, monkeypatch):
    """GET /v1/dev/captures/{id} returns the full event list for that capture."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", True)

    tid, key = _tenant("byid")
    payload = _gmp_payload(
        "byid_user@example.test",
        [_account("ABCD-0001", "Hilltop")],
    )
    r = client.post("/v1/sync", json=payload, headers=_bearer(key))
    assert r.status_code == 200

    list_r = client.get("/v1/dev/captures", headers=_auth(tid))
    assert list_r.status_code == 200
    captures = list_r.json()["captures"]
    assert len(captures) >= 1
    cid = captures[0]["capture_id"]

    detail_r = client.get(f"/v1/dev/captures/{cid}", headers=_auth(tid))
    assert detail_r.status_code == 200
    body = detail_r.json()
    assert body["capture_id"] == cid
    assert len(body["events"]) >= 1


# ─── test: privacy — auth token never stored ───────────────────────────────

def test_payload_excerpt_strips_auth(client, monkeypatch):
    """auth.apiToken must never appear in payload_excerpt stored in the DB."""
    import api.dev_sandbox as ds
    monkeypatch.setattr(ds, "DEV_ENABLED", True)

    tid, key = _tenant("priv")
    secret_token = "SUPER_SECRET_TOKEN_" + secrets.token_hex(8)
    payload = _gmp_payload("priv_user@example.test", [_account("PRIV-0001", "Private Array")])
    payload["auth"]["apiToken"] = secret_token

    r = client.post("/v1/sync", json=payload, headers=_bearer(key))
    assert r.status_code == 200

    with SessionLocal() as db:
        events = db.execute(
            select(CaptureEvent).where(CaptureEvent.tenant_id == tid)
        ).scalars().all()

    for ev in events:
        if ev.payload_excerpt:
            excerpt_str = json.dumps(ev.payload_excerpt)
            assert secret_token not in excerpt_str, (
                f"secret token leaked into payload_excerpt for stage {ev.stage}"
            )
