"""HTTP smoke tests for Performance Verification routes.

Auth required on all endpoints. Uses tenant-key bearer (array_owners dual-auth).
Does not invent measured energy — summary/report may return available=False.
"""
from __future__ import annotations

import secrets

import pytest

from api.db import SessionLocal
from api.models import Tenant


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="PV Routes Test",
            contact_email=f"{key}@t.test",
            tenant_key=key,
            plan="standard",
            active=True,
            product="array_operator",
        ))
        db.commit()
    return tid, key


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_import_router():
    from api.perf_verification.routes import router
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/v1/array-owners/verification/method" in paths
    assert "/v1/array-owners/verification/summary" in paths
    assert "/v1/array-owners/verification/report.pdf" in paths


def test_method_without_auth_is_401_or_403(client):
    resp = client.get("/v1/array-owners/verification/method")
    assert resp.status_code in (401, 403)


def test_summary_without_auth_is_401_or_403(client):
    resp = client.get("/v1/array-owners/verification/summary")
    assert resp.status_code in (401, 403)


def test_method_with_tenant_key(client):
    _tid, key = _make_tenant()
    resp = client.get(
        "/v1/array-owners/verification/method",
        headers=_auth(key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "method" in body
    assert "footer" in body
    assert body["method"].get("title") or body["method"].get("standards")
    assert body.get("default_deviation_threshold") == 0.05


def test_settings_get_and_put(client):
    _tid, key = _make_tenant()
    r = client.get(
        "/v1/array-owners/verification/settings",
        headers=_auth(key),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["deviation_threshold"] == 0.05

    r2 = client.put(
        "/v1/array-owners/verification/settings",
        headers=_auth(key),
        json={"enabled": False, "deviation_threshold": 0.08},
    )
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False
    assert r2.json()["deviation_threshold"] == 0.08

    r3 = client.get(
        "/v1/array-owners/verification/settings",
        headers=_auth(key),
    )
    assert r3.json()["enabled"] is False
    assert r3.json()["deviation_threshold"] == 0.08


def test_summary_empty_fleet_honest(client):
    _tid, key = _make_tenant()
    resp = client.get(
        "/v1/array-owners/verification/summary?window_days=14",
        headers=_auth(key),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Empty fleet: available may be False; never invent PI numbers.
    assert "portfolio" in body or body.get("available") is False
    if body.get("available"):
        assert "performance_index" in (body.get("portfolio") or {})
    else:
        # honest empty
        port = body.get("portfolio") or {}
        assert port.get("performance_index") is None or port.get("array_count", 0) == 0


def test_report_json_period(client):
    _tid, key = _make_tenant()
    resp = client.get(
        "/v1/array-owners/verification/report?period=2026-06",
        headers=_auth(key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("period") == "2026-06"


def test_report_bad_period(client):
    _tid, key = _make_tenant()
    resp = client.get(
        "/v1/array-owners/verification/report?period=not-a-month",
        headers=_auth(key),
    )
    assert resp.status_code == 400


def test_intervention_missing_ticket(client):
    _tid, key = _make_tenant()
    resp = client.get(
        "/v1/array-owners/verification/interventions/999999",
        headers=_auth(key),
    )
    assert resp.status_code == 404


def test_p2_stubs_planned():
    from api.perf_verification import p2_stubs

    assert p2_stubs.om_multi_tenant_status()["status"] == "planned"
    assert p2_stubs.sla_packaging_status()["status"] == "planned"
    with pytest.raises(NotImplementedError):
        p2_stubs.om_verification_summary()
    with pytest.raises(NotImplementedError):
        p2_stubs.build_sla_pack()
