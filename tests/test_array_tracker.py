"""Operator/tenant-level generation-spreadsheet tracker — HTTP endpoint tests.

Covers the additive `/v1/array-operator/tracker` surface (api/array_tracker.py):
  * GET returns {enabled:false} when the feature flag is off (UI hides)
  * with the flag on: GET enabled:true/has_sheet:false before upload
  * POST detects an arbitrary-column sheet, stores it per-TENANT, returns the
    same shape as GET (has_sheet:true + detected columns/headers)
  * GET reflects the stored sheet
  * download streams an xlsx
  * DELETE detaches → {enabled:true, has_sheet:false}
  * cross-tenant isolation: tenant B sees its OWN (empty) tracker, not A's
  * demo tenant is refused on the mutating routes

The detection logic itself is covered by tests/test_sheet_tracker.py; this file
exercises the tenant-keyed storage + auth + flag-gating wiring.
"""
from __future__ import annotations

import io
import secrets

import openpyxl
import pytest
from fastapi.testclient import TestClient

from api.db import SessionLocal
from api.models import Tenant


def _make_tenant(**over) -> str:
    """Create an AO tenant and return a SIGNED dashboard session token (the
    Bearer the `/v1/array-operator/tracker` routes expect via
    account.tenant_from_session)."""
    from api.account import mint_session_for_tenant
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    fields = dict(
        id=tid, name="Op Test", contact_email=f"{key}@op.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
    )
    fields.update(over)
    with SessionLocal() as db:
        db.add(Tenant(**fields))
        db.commit()
    return mint_session_for_tenant(tid)


def _sheet_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MyLedger"
    rows = [
        ["Maple Farm Solar — Generation Log", None, None, None, None],
        ["Billing Month", "Solar Produced (kWh)", "Home Usage",
         "Credit $/kWh", "Total Credit"],
        ["2026-03", 1820.5, 940, 0.2576, 469.0],
        ["2026-04", 2010.0, 880, 0.2576, 517.8],
    ]
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def client():
    from api.app import app
    return TestClient(app)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _enable(monkeypatch):
    monkeypatch.setenv("SPREADSHEET_TRACKER_ENABLED", "true")


def test_flag_off_returns_disabled(client, monkeypatch):
    monkeypatch.delenv("SPREADSHEET_TRACKER_ENABLED", raising=False)
    key = _make_tenant()
    r = client.get("/v1/array-operator/tracker", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.json()["tracker"] == {"enabled": False}
    # mutating routes 404 while the flag is off
    assert client.delete("/v1/array-operator/tracker",
                         headers=_auth(key)).status_code == 404


def test_full_lifecycle(client, monkeypatch):
    _enable(monkeypatch)
    key = _make_tenant()

    # before upload: enabled, no sheet
    r = client.get("/v1/array-operator/tracker", headers=_auth(key))
    assert r.status_code == 200, r.text
    tr = r.json()["tracker"]
    assert tr["enabled"] is True
    assert tr["has_sheet"] is False

    # upload an arbitrary-column sheet
    blob = _sheet_bytes()
    r = client.post(
        "/v1/array-operator/tracker", headers=_auth(key),
        files={"file": ("maple.xlsx", blob,
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    tr = r.json()["tracker"]
    assert tr["has_sheet"] is True
    assert tr["filename"] == "maple.xlsx"
    # detection ran — generation column found, last period read
    assert tr["columns"]["generation"] == 1
    assert tr["columns"]["period"] == 0
    assert tr["last_period"] == "2026-04"
    assert tr["updated_at"]

    # GET reflects the stored sheet
    r = client.get("/v1/array-operator/tracker", headers=_auth(key))
    assert r.json()["tracker"]["has_sheet"] is True

    # download streams an xlsx
    r = client.get("/v1/array-operator/tracker/download", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats")
    # it's a real workbook
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.active.max_row >= 4

    # delete detaches
    r = client.delete("/v1/array-operator/tracker", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.json()["tracker"] == {
        k: v for k, v in r.json()["tracker"].items()
    }  # full shape
    tr = r.json()["tracker"]
    assert tr["enabled"] is True
    assert tr["has_sheet"] is False

    # download now 404s
    assert client.get("/v1/array-operator/tracker/download",
                      headers=_auth(key)).status_code == 404


def test_cross_tenant_isolation(client, monkeypatch):
    _enable(monkeypatch)
    key_a = _make_tenant()
    key_b = _make_tenant()
    # A uploads
    client.post(
        "/v1/array-operator/tracker", headers=_auth(key_a),
        files={"file": ("a.xlsx", _sheet_bytes(),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")})
    # B sees its own empty tracker, never A's sheet
    r = client.get("/v1/array-operator/tracker", headers=_auth(key_b))
    assert r.json()["tracker"]["has_sheet"] is False
    assert client.get("/v1/array-operator/tracker/download",
                      headers=_auth(key_b)).status_code == 404


def test_demo_tenant_refused_on_upload(client, monkeypatch):
    _enable(monkeypatch)
    key = _make_tenant(is_demo=True)
    r = client.post(
        "/v1/array-operator/tracker", headers=_auth(key),
        files={"file": ("a.xlsx", _sheet_bytes(),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")})
    assert r.status_code == 403, r.text


def test_bad_upload_rejected(client, monkeypatch):
    _enable(monkeypatch)
    key = _make_tenant()
    # not an xlsx/csv → unreadable → 422 (detection couldn't find a gen column)
    r = client.post(
        "/v1/array-operator/tracker", headers=_auth(key),
        files={"file": ("junk.csv", b"hello world no columns here\n",
                        "text/csv")})
    assert r.status_code == 422, r.text
