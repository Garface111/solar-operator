"""Exclude-from-fleet + honest fleet-trends rate provenance (Colleen low-hanging)."""
from datetime import date

from api.db import SessionLocal
from api.models import Array, DailyGeneration, Tenant, UtilityAccount
from api.account import _sign_session
from fastapi.testclient import TestClient
from api.app import app


def _make_tenant(product="array_operator"):
    import secrets
    tid = "ten_" + secrets.token_hex(8)
    key = "sol_live_" + secrets.token_hex(16)
    with SessionLocal() as db:
        t = Tenant(id=tid, tenant_key=key, name="T", contact_email=f"{tid}@t.test",
                   product=product, active=True)
        db.add(t)
        db.commit()
    return tid, key


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _make_array(tid, name):
    with SessionLocal() as db:
        a = Array(tenant_id=tid, name=name, fuel_type="solar")
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id


def _add_daily(tid, aid, pairs):
    with SessionLocal() as db:
        for d, k in pairs:
            db.add(DailyGeneration(tenant_id=tid, array_id=aid, day=d, kwh=k))
        db.commit()


client = TestClient(app)


def test_exclude_array_hides_from_fleet_tree_and_trends():
    tid, key = _make_tenant()
    keep = _make_array(tid, "Business")
    personal = _make_array(tid, "River")
    _add_daily(tid, keep, [(date(2026, 3, 1), 10.0)])
    _add_daily(tid, personal, [(date(2026, 3, 1), 999.0)])

    r = client.post(
        f"/v1/array-owners/arrays/{personal}/exclude",
        headers=_auth(key),
        json={"excluded": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["excluded"] is True

    tr = client.get("/v1/array-owners/fleet-trends", headers=_auth(key))
    assert tr.status_code == 200
    b = tr.json()
    assert b["lifetime_kwh"] == 10.0
    assert [a["name"] for a in b["by_array"]] == ["Business"]
    # Honest provenance fields always present when a rate is computed
    if b.get("blended_rate_usd_per_kwh"):
        assert b.get("rate_source") in (
            "vt_utility_default", "tenant_default", "bill_or_schedule", "mixed"
        )
        assert b.get("rate_note")


def test_exclude_array_reinclude():
    tid, key = _make_tenant()
    aid = _make_array(tid, "River")
    r = client.post(
        f"/v1/array-owners/arrays/{aid}/exclude",
        headers=_auth(key),
        json={"excluded": True},
    )
    assert r.status_code == 200
    r2 = client.post(
        f"/v1/array-owners/arrays/{aid}/exclude",
        headers=_auth(key),
        json={"excluded": False},
    )
    assert r2.status_code == 200
    assert r2.json()["excluded"] is False
    with SessionLocal() as db:
        assert db.get(Array, aid).excluded is False
