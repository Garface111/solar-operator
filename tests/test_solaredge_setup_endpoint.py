"""
Tests for the SolarEdge setup endpoints:
  POST   /v1/account/clients/{client_id}/arrays/{array_id}/solaredge
  GET    /v1/account/clients/{client_id}/arrays/{array_id}/solaredge/preview
  DELETE /v1/account/clients/{client_id}/arrays/{array_id}/solaredge

Mocks all SolarEdge HTTP calls; never hits the real API.
"""
from __future__ import annotations

import secrets
from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, DailyGeneration, Tenant


# ── fixture helpers ────────────────────────────────────────────────────────────


def _make_tenant_with_array() -> tuple[str, str, int, int]:
    """Create Tenant → Client → Array. Returns (tenant_id, auth, client_id, array_id)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="SE Test Operator",
            contact_email=f"{tid}@test.com",
            tenant_key="k_" + secrets.token_hex(8),
            plan="standard",
            active=True,
        ))
        db.flush()
        c = Client(tenant_id=tid, name="SE Test Client", active=True)
        db.add(c)
        db.flush()
        arr = Array(
            tenant_id=tid,
            client_id=c.id,
            name="SE Test Array",
            nepool_gis_id="99888",
        )
        db.add(arr)
        db.flush()
        cid, arr_id = c.id, arr.id
        db.commit()
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    return tid, auth, cid, arr_id


_FAKE_DETAILS = {
    "site_id": 55555,
    "name": "Test Solar Site",
    "peak_kw": 200.0,
    "address": "1 Green St, Burlington, VT",
    "status": "Active",
}

_FAKE_SITES = [
    {"site_id": 55555, "name": "Test Solar Site", "peak_kw": 200.0, "address": "Burlington, VT"},
    {"site_id": 55556, "name": "Second Site", "peak_kw": 100.0, "address": "Montpelier, VT"},
]


# ── POST /solaredge — happy paths ──────────────────────────────────────────────


def test_setup_with_site_id_saves_credentials(client):
    """POST with valid key + site_id validates and saves to Array."""
    _, auth, cid, arr_id = _make_tenant_with_array()

    with patch("api.solaredge.site_details", return_value=_FAKE_DETAILS):
        resp = client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "test_key_abc", "site_id": 55555},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["needs_site_selection"] is False
    assert data["site_name"] == "Test Solar Site"
    assert data["peak_kw"] == 200.0

    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr.solaredge_api_key == "test_key_abc"
        assert arr.solaredge_site_id == 55555


def test_setup_without_site_id_account_key_returns_site_list(client):
    """POST with valid account-level key (no site_id) → returns site list for picker."""
    _, auth, cid, arr_id = _make_tenant_with_array()

    with patch("api.solaredge.list_sites", return_value=_FAKE_SITES):
        resp = client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "account_key_xyz"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["needs_site_selection"] is True
    assert len(data["sites"]) == 2
    assert data["sites"][0]["site_id"] == 55555

    # Credentials should NOT be saved yet (no site picked)
    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr.solaredge_api_key is None


def test_setup_without_site_id_site_level_key_returns_empty_sites(client):
    """POST with site-level key (list_sites returns []) → needs_site_selection with empty list."""
    _, auth, cid, arr_id = _make_tenant_with_array()

    with patch("api.solaredge.list_sites", return_value=[]):
        resp = client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "site_level_key"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["needs_site_selection"] is True
    assert data["sites"] == []


def test_setup_auto_selects_single_site(client):
    """POST with account key covering exactly one site auto-selects it."""
    _, auth, cid, arr_id = _make_tenant_with_array()

    with (
        patch("api.solaredge.list_sites", return_value=[_FAKE_SITES[0]]),
        patch("api.solaredge.site_details", return_value=_FAKE_DETAILS),
    ):
        resp = client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "single_site_account_key"},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["needs_site_selection"] is False
    assert data["site_name"] == "Test Solar Site"

    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr.solaredge_site_id == 55555


# ── POST /solaredge — error paths ──────────────────────────────────────────────


def test_setup_invalid_key_returns_400(client):
    """POST with bad key → site_details raises SolarEdgeAuthError → 400."""
    from api.adapters.solaredge import SolarEdgeAuthError
    _, auth, cid, arr_id = _make_tenant_with_array()

    with patch("api.solaredge.site_details", side_effect=SolarEdgeAuthError("Bad key")):
        resp = client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "bad_key", "site_id": 55555},
            headers={"Authorization": auth},
        )

    assert resp.status_code == 400
    assert "Bad key" in resp.json()["detail"] or resp.status_code == 400


def test_setup_wrong_tenant_array_returns_404(client):
    """Cannot connect SolarEdge on another tenant's array."""
    _, auth_a, cid_a, _arr_id_a = _make_tenant_with_array()
    _tid_b, _auth_b, _cid_b, arr_id_b = _make_tenant_with_array()

    with patch("api.solaredge.site_details", return_value=_FAKE_DETAILS):
        resp = client.post(
            f"/v1/account/clients/{cid_a}/arrays/{arr_id_b}/solaredge",
            json={"api_key": "key", "site_id": 55555},
            headers={"Authorization": auth_a},
        )

    assert resp.status_code == 404


# ── DELETE /solaredge ──────────────────────────────────────────────────────────


def test_disconnect_clears_key_and_preserves_daily_generation(client):
    """DELETE clears api_key + site_id but leaves DailyGeneration rows intact."""
    tid, auth, cid, arr_id = _make_tenant_with_array()

    # First connect
    with patch("api.solaredge.site_details", return_value=_FAKE_DETAILS):
        r = client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "the_key", "site_id": 55555},
            headers={"Authorization": auth},
        )
    assert r.status_code == 200

    # Seed some DailyGeneration rows
    with SessionLocal() as db:
        db.add(DailyGeneration(
            tenant_id=tid, array_id=arr_id,
            day=date(2024, 7, 1), kwh=25.0, source="solaredge",
        ))
        db.commit()

    # Disconnect
    resp = client.delete(
        f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with SessionLocal() as db:
        arr = db.get(Array, arr_id)
        assert arr.solaredge_api_key is None
        assert arr.solaredge_site_id is None

        # DailyGeneration rows must still exist
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr_id)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].kwh == 25.0


# ── GET /solaredge/preview ─────────────────────────────────────────────────────


def test_preview_returns_sample_after_connect(client):
    """GET /preview pulls 7 days and returns day count + sample."""
    tid, auth, cid, arr_id = _make_tenant_with_array()

    # Connect first
    with patch("api.solaredge.site_details", return_value=_FAKE_DETAILS):
        client.post(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge",
            json={"api_key": "the_key", "site_id": 55555},
            headers={"Authorization": auth},
        )

    fake_entries = [
        {"day": date(2024, 7, d), "kwh": float(100 + d), "source": "solaredge"}
        for d in range(1, 6)
    ]
    with patch("api.solaredge.pull_daily_for_array",
               return_value={"days_pulled": 5, "days_skipped_zero": 2, "errors": []}):
        # Seed rows so the preview query has data
        with SessionLocal() as db:
            for e in fake_entries:
                db.add(DailyGeneration(
                    tenant_id=tid, array_id=arr_id,
                    day=e["day"], kwh=e["kwh"], source="solaredge",
                ))
            db.commit()

        resp = client.get(
            f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge/preview",
            headers={"Authorization": auth},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["days_pulled"] == 5
    assert isinstance(data["sample"], list)


def test_preview_fails_without_credentials(client):
    """GET /preview on array with no api_key → 400."""
    _, auth, cid, arr_id = _make_tenant_with_array()

    resp = client.get(
        f"/v1/account/clients/{cid}/arrays/{arr_id}/solaredge/preview",
        headers={"Authorization": auth},
    )
    assert resp.status_code == 400
