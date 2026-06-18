"""Test the daily-series endpoint that powers the daily-generation bar graph."""
from __future__ import annotations

import secrets
from datetime import date

from api.db import SessionLocal
from api.models import Tenant, Client, Array, DailyGeneration, BillingReportSubscription


def _seed_with_daily():
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Bars Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="C", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Bars Array", client_id=c.id, fuel_type="solar",
                    region="central"); db.add(arr); db.flush()
        # 10 days of real generation in May 2025
        for dom in range(1, 11):
            db.add(DailyGeneration(tenant_id=tid, array_id=arr.id,
                                   day=date(2025, 5, dom), kwh=100.0 + dom, source="manual"))
        sub = BillingReportSubscription(
            tenant_id=tid, billing_model="percent_of_array",
            customer_name="Half Offtaker", array_id=arr.id, allocation_pct=0.5,
            cadence="monthly", enabled=True)
        db.add(sub); db.flush()
        sid = sub.id
        db.commit()
        return tid, sid


def _auth(tid):
    from api.account import mint_session_for_tenant
    return "Bearer " + mint_session_for_tenant(tid)


def test_daily_series_returns_real_scaled_points(client):
    tid, sid = _seed_with_daily()
    auth = _auth(tid)
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sid}/daily-series",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["has_data"] is True
    assert d["period_label"] == "May 2025"
    assert len(d["points"]) == 10
    # allocation 0.5 → offtaker share is half the array kWh
    p0 = d["points"][0]
    assert p0["array_kwh"] == 101.0
    assert p0["kwh"] == 50.5
    # total = sum of scaled points
    assert abs(d["total_kwh"] - sum(p["kwh"] for p in d["points"])) < 0.1


def test_daily_series_explicit_month(client):
    tid, sid = _seed_with_daily()
    auth = _auth(tid)
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sid}/daily-series?period=2025-05",
                   headers={"Authorization": auth})
    assert r.status_code == 200
    assert r.json()["period_start"] == "2025-05-01"
    # a month with no data → honest empty, not fabricated
    r2 = client.get(f"/v1/array-operator/billing/subscriptions/{sid}/daily-series?period=2025-01",
                    headers={"Authorization": auth})
    assert r2.status_code == 200
    assert r2.json()["has_data"] is False
    assert r2.json()["points"] == []


def test_daily_series_bad_period_400(client):
    tid, sid = _seed_with_daily()
    auth = _auth(tid)
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sid}/daily-series?period=nonsense",
                   headers={"Authorization": auth})
    assert r.status_code == 400
