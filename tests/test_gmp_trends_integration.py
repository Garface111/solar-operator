"""Trends + GMP daily sponge integration: fleet-trends must surface GMP daily
generation (not just the CSV DailyGeneration table), without double-counting a
day that both sources cover."""
from __future__ import annotations

import secrets
from datetime import date

import pytest

from api.db import SessionLocal
from api.models import (
    Tenant, Client, Array, UtilityAccount, DailyGeneration, GmpDailyGeneration,
)


_SEEDED: list[str] = []


def _cleanup():
    if not _SEEDED:
        return
    with SessionLocal() as db:
        for tid in _SEEDED:
            for Model in (GmpDailyGeneration, DailyGeneration, UtilityAccount, Array, Client):
                for row in db.query(Model).filter(Model.tenant_id == tid).all():
                    db.delete(row)
            t = db.get(Tenant, tid)
            if t is not None:
                db.delete(t)
        db.commit()
    _SEEDED.clear()


@pytest.fixture(autouse=True)
def _isolate():
    yield
    _cleanup()


def _seed():
    tid = "ten_" + secrets.token_hex(6)
    _SEEDED.append(tid)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="GMP Trends Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="C", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="GMP Array", client_id=c.id, fuel_type="solar",
                    region="central"); db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="999000111", enabled=True)
        db.add(acct); db.flush()
        # GMP daily sponge: 3 days
        for dom, kwh in [(1, 100.0), (2, 110.0), (3, 120.0)]:
            db.add(GmpDailyGeneration(tenant_id=tid, account_id=acct.id, array_id=arr.id,
                                      account_number=acct.account_number,
                                      day=date(2025, 7, dom), kwh=kwh,
                                      interval_count=96, source="gmp_api"))
        # CSV DailyGeneration: overlaps day 3 (different value) + adds day 4
        db.add(DailyGeneration(tenant_id=tid, array_id=arr.id, day=date(2025, 7, 3),
                               kwh=200.0, source="manual"))
        db.add(DailyGeneration(tenant_id=tid, array_id=arr.id, day=date(2025, 7, 4),
                               kwh=130.0, source="manual"))
        db.commit()
        return tid, arr.id


def test_fleet_trends_includes_gmp_and_prefers_csv_on_overlap(client):
    """fleet-trends must total: day1+day2 from GMP (100+110), day3 from CSV (200,
    NOT 120+200), day4 from CSV (130) = 540. Proves GMP data is surfaced AND not
    double-counted where the CSV table already covers a day."""
    tid, array_id = _seed()
    from api.account import mint_session_for_tenant
    auth = "Bearer " + mint_session_for_tenant(tid)
    r = client.get("/v1/array-owners/fleet-trends", headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = r.json()
    # lifetime = 100 + 110 + 200(csv wins over gmp 120) + 130 = 540
    assert d["lifetime_kwh"] == pytest.approx(540.0, abs=0.1), d["lifetime_kwh"]
    # July 2025 present in monthly_by_year
    assert "2025" in d["monthly_by_year"]
    jul = [m for m in d["monthly_by_year"]["2025"] if m["month"] == 7]
    assert jul and jul[0]["kwh"] == pytest.approx(540.0, abs=0.1)


def test_fleet_trends_pure_gmp_array_shows_data(client):
    """An array with ONLY GMP data (no CSV rows) must still appear in trends."""
    tid = "ten_" + secrets.token_hex(6)
    _SEEDED.append(tid)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Pure GMP", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="C", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Pure GMP Array", client_id=c.id,
                    fuel_type="solar", region="central"); db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="999000222", enabled=True)
        db.add(acct); db.flush()
        db.add(GmpDailyGeneration(tenant_id=tid, account_id=acct.id, array_id=arr.id,
                                  account_number=acct.account_number,
                                  day=date(2025, 6, 15), kwh=88.0,
                                  interval_count=96, source="gmp_api"))
        db.commit()
        aid = arr.id
    from api.account import mint_session_for_tenant
    auth = "Bearer " + mint_session_for_tenant(tid)
    r = client.get("/v1/array-owners/fleet-trends", headers={"Authorization": auth})
    assert r.status_code == 200
    d = r.json()
    assert d["lifetime_kwh"] == pytest.approx(88.0, abs=0.1)
    by_arr = {a["array_id"]: a for a in d.get("by_array", [])}
    assert aid in by_arr and by_arr[aid]["lifetime_kwh"] == pytest.approx(88.0, abs=0.1)


def test_fleet_trends_array_filter_scopes_payload(client):
    """?array_id=N scopes the aggregates to ONE array, but by_array still lists
    the whole fleet (so the filter dropdown can switch). Bad id -> 404."""
    tid = "ten_" + secrets.token_hex(6)
    _SEEDED.append(tid)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Filter Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="C", active=True); db.add(c); db.flush()
        a1 = Array(tenant_id=tid, name="Array One", client_id=c.id, fuel_type="solar",
                   region="central"); db.add(a1)
        a2 = Array(tenant_id=tid, name="Array Two", client_id=c.id, fuel_type="solar",
                   region="central"); db.add(a2); db.flush()
        db.add(DailyGeneration(tenant_id=tid, array_id=a1.id, day=date(2025, 7, 1),
                               kwh=100.0, source="manual"))
        db.add(DailyGeneration(tenant_id=tid, array_id=a2.id, day=date(2025, 7, 1),
                               kwh=300.0, source="manual"))
        db.commit()
        a1id = a1.id
    from api.account import mint_session_for_tenant
    auth = {"Authorization": "Bearer " + mint_session_for_tenant(tid)}

    rf = client.get("/v1/array-owners/fleet-trends", headers=auth)
    assert rf.status_code == 200
    assert rf.json()["lifetime_kwh"] == pytest.approx(400.0, abs=0.1)
    assert rf.json()["selected_array_id"] is None

    r1 = client.get(f"/v1/array-owners/fleet-trends?array_id={a1id}", headers=auth)
    assert r1.status_code == 200
    body = r1.json()
    assert body["lifetime_kwh"] == pytest.approx(100.0, abs=0.1)
    assert body["selected_array_id"] == a1id
    assert len(body["by_array"]) == 2, "by_array must stay full-fleet for the dropdown"

    r404 = client.get("/v1/array-owners/fleet-trends?array_id=99999999", headers=auth)
    assert r404.status_code == 404


def test_gmp_backfill_admin_requires_key(client, monkeypatch):
    """The trigger endpoints must be admin-guarded: when ADMIN_API_KEY is set, a
    request with the wrong/no key is rejected. (In local dev with no key set the
    guard intentionally falls open; in prod/_ON_RAILWAY it fails closed.)"""
    import api.app as appmod
    monkeypatch.setattr(appmod, "ADMIN_API_KEY", "secret-test-key", raising=False)
    # no key → 403
    r = client.post("/admin/gmp-backfill/tenant/ten_whatever")
    assert r.status_code == 403, r.status_code
    # wrong key → 403
    r2 = client.post("/admin/gmp-backfill/tenant/ten_whatever",
                     headers={"X-Admin-Key": "nope"})
    assert r2.status_code == 403, r2.status_code
