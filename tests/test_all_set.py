"""
Tests for the "You're all set!" milestone:
  - _compute_all_set unit tests (all four edge cases)
  - GET /v1/account exposes onboarding_array_estimate + all_set fields
"""
from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from api.account import _compute_all_set, mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tenant(*, estimate: int | None = None) -> tuple[str, str]:
    """Create a tenant and return (tenant_id, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(12)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="AllSet Test Tenant",
            contact_email=f"{tid}@allset.test",
            tenant_key=key,
            plan="standard",
            active=True,
            onboarding_array_estimate=estimate,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _add_client(tid: str) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="Client " + secrets.token_hex(3), active=True)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def _add_array(tid: str, client_id: int) -> None:
    with SessionLocal() as db:
        db.add(Array(
            tenant_id=tid,
            client_id=client_id,
            name="Array " + secrets.token_hex(3),
        ))
        db.commit()


# ── _compute_all_set unit tests ───────────────────────────────────────────────


def test_all_set_false_when_estimate_null():
    tid, _ = _make_tenant(estimate=None)
    with SessionLocal() as db:
        assert _compute_all_set(db, tid) is False


def test_all_set_false_when_arrays_below_estimate():
    tid, _ = _make_tenant(estimate=3)
    cid = _add_client(tid)
    _add_array(tid, cid)  # only 1 of 3
    with SessionLocal() as db:
        assert _compute_all_set(db, tid) is False


def test_all_set_true_when_arrays_meet_estimate():
    tid, _ = _make_tenant(estimate=2)
    cid = _add_client(tid)
    _add_array(tid, cid)
    _add_array(tid, cid)  # exactly 2 of 2
    with SessionLocal() as db:
        assert _compute_all_set(db, tid) is True


def test_all_set_false_when_estimate_set_but_zero_clients():
    tid, _ = _make_tenant(estimate=2)
    # No clients added — no arrays either.
    with SessionLocal() as db:
        assert _compute_all_set(db, tid) is False


# ── GET /v1/account exposes the two new fields ────────────────────────────────


def test_account_endpoint_exposes_all_set_fields(client: TestClient):
    tid, auth = _make_tenant(estimate=1)
    cid = _add_client(tid)
    _add_array(tid, cid)

    resp = client.get("/v1/account", headers={"Authorization": auth})
    assert resp.status_code == 200
    body = resp.json()
    assert "onboarding_array_estimate" in body
    assert "all_set" in body
    assert body["onboarding_array_estimate"] == 1
    assert body["all_set"] is True


def test_account_endpoint_all_set_false_when_estimate_null(client: TestClient):
    tid, auth = _make_tenant(estimate=None)
    resp = client.get("/v1/account", headers={"Authorization": auth})
    assert resp.status_code == 200
    body = resp.json()
    assert body["onboarding_array_estimate"] is None
    assert body["all_set"] is False
