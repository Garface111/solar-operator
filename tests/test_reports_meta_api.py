"""
Tests for reports meta endpoints:
  POST /v1/account/reports/send-mode
  GET  /v1/account/reports/next-run
"""
from __future__ import annotations

import secrets
from datetime import date

import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tenant(*, frequency: str = "quarterly") -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Meta Test Co",
            contact_email=f"{tid}@meta.test",
            tenant_key=key,
            plan="standard",
            active=True,
            report_frequency=frequency,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _add_client_and_array(tenant_id: str) -> tuple[int, int]:
    with SessionLocal() as db:
        c = Client(tenant_id=tenant_id, name="Next-run Client", active=True)
        db.add(c)
        db.flush()
        a = Array(tenant_id=tenant_id, client_id=c.id, name="Array A")
        db.add(a)
        db.commit()
        db.refresh(c)
        db.refresh(a)
        return c.id, a.id


# ── send-mode ─────────────────────────────────────────────────────────────────


def test_send_mode_put_round_trip(client):
    for mode in ("to_me", "to_client", "to_both"):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/reports/send-mode",
            json={"send_mode": mode},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200, f"mode={mode!r}: {resp.text}"
        data = resp.json()
        assert data["ok"] is True
        assert data["send_mode"] == mode


def test_send_mode_persists(client):
    tid, auth = _make_tenant()
    client.post(
        "/v1/account/reports/send-mode",
        json={"send_mode": "to_both"},
        headers={"Authorization": auth},
    )
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        assert t.send_mode == "to_both"


def test_send_mode_rejects_invalid(client):
    _, auth = _make_tenant()
    resp = client.post(
        "/v1/account/reports/send-mode",
        json={"send_mode": "spam_everyone"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400, resp.text


def test_send_mode_rejects_empty(client):
    _, auth = _make_tenant()
    resp = client.post(
        "/v1/account/reports/send-mode",
        json={"send_mode": ""},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400, resp.text


# ── next-run ──────────────────────────────────────────────────────────────────


def test_next_run_returns_future_date(client):
    tid, auth = _make_tenant(frequency="quarterly")
    _add_client_and_array(tid)
    resp = client.get("/v1/account/reports/next-run",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Shape check
    for field in ("next_run_date", "days_until", "frequency",
                  "array_count", "mwh_preview", "client_count"):
        assert field in data, f"missing field: {field!r}"

    # next_run_date should be a valid ISO date in the future (or today)
    next_run = date.fromisoformat(data["next_run_date"])
    assert next_run >= date.today(), f"next_run_date {next_run} is in the past"
    assert data["days_until"] >= 0
    assert data["frequency"] == "quarterly"
    assert data["array_count"] >= 0
    assert isinstance(data["mwh_preview"], (int, float))


def test_next_run_monthly_frequency(client):
    tid, auth = _make_tenant(frequency="monthly")
    resp = client.get("/v1/account/reports/next-run",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frequency"] == "monthly"
    next_run = date.fromisoformat(data["next_run_date"])
    # Monthly: first of next month — day must be 1
    assert next_run.day == 1
    assert next_run > date.today()


def test_next_run_no_arrays(client):
    """Tenant with no arrays: smoke-test that it doesn't 500."""
    _, auth = _make_tenant(frequency="quarterly")
    resp = client.get("/v1/account/reports/next-run",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["array_count"] == 0
    assert data["mwh_preview"] == 0.0
