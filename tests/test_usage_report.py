"""Tests for the Array Operator per-kWh usage-report job (api/jobs/usage_report).

Covers the pure DB summation (tenant_period_kwh) that drives metered billing —
that it sums only the tenant's own billable arrays over the billing window and
excludes soft-deleted / excluded arrays. The Stripe-reporting wrapper is not
exercised here (no live Stripe in tests); the summation is the part that decides
what an owner is billed.
"""
from datetime import date, timedelta

from api.db import SessionLocal
from api.jobs.usage_report import tenant_period_kwh
from api.models import Tenant, Array, DailyGeneration, now


def _mk_tenant(db, tid: str) -> Tenant:
    t = Tenant(
        id=tid, name="Owner " + tid, contact_email=f"{tid}@example.com",
        tenant_key="key_" + tid, plan="standard", active=True, created_at=now(),
        product="array_operator", subscription_status="active",
    )
    db.add(t)
    return t


def _mk_array(db, tid, name, *, excluded=False, deleted=False) -> Array:
    a = Array(tenant_id=tid, name=name, excluded=excluded)
    if deleted:
        a.deleted_at = now()
    db.add(a)
    db.flush()
    return a


def _add_kwh(db, tid, array_id, day, kwh):
    db.add(DailyGeneration(tenant_id=tid, array_id=array_id, day=day, kwh=kwh))


def test_period_kwh_sums_billable_arrays_only():
    today = date.today()
    since = today.replace(day=1)
    with SessionLocal() as db:
        t = _mk_tenant(db, "ten_usage_a")
        a1 = _mk_array(db, t.id, "Roof A")
        a2 = _mk_array(db, t.id, "Roof B")
        excl = _mk_array(db, t.id, "Roof Excl", excluded=True)
        gone = _mk_array(db, t.id, "Roof Gone", deleted=True)
        _add_kwh(db, t.id, a1.id, since, 100.0)
        _add_kwh(db, t.id, a1.id, since + timedelta(days=1), 150.0)
        _add_kwh(db, t.id, a2.id, since, 200.0)
        _add_kwh(db, t.id, excl.id, since, 999.0)   # excluded → not billed
        _add_kwh(db, t.id, gone.id, since, 999.0)   # soft-deleted → not billed
        db.commit()

        total = tenant_period_kwh(db, t.id, since)
    assert total == 450.0  # 100 + 150 + 200, excluding the excluded/deleted ones


def test_period_kwh_respects_since_date():
    today = date.today()
    since = today.replace(day=1)
    before = since - timedelta(days=5)
    with SessionLocal() as db:
        t = _mk_tenant(db, "ten_usage_b")
        a1 = _mk_array(db, t.id, "Roof A")
        _add_kwh(db, t.id, a1.id, before, 500.0)   # before the window → excluded
        _add_kwh(db, t.id, a1.id, since, 75.0)      # in the window
        db.commit()
        total = tenant_period_kwh(db, t.id, since)
    assert total == 75.0


def test_period_kwh_isolates_tenants():
    today = date.today()
    since = today.replace(day=1)
    with SessionLocal() as db:
        t1 = _mk_tenant(db, "ten_usage_c1")
        t2 = _mk_tenant(db, "ten_usage_c2")
        a1 = _mk_array(db, t1.id, "C1 Roof")
        a2 = _mk_array(db, t2.id, "C2 Roof")
        _add_kwh(db, t1.id, a1.id, since, 300.0)
        _add_kwh(db, t2.id, a2.id, since, 80.0)
        db.commit()
        assert tenant_period_kwh(db, t1.id, since) == 300.0
        assert tenant_period_kwh(db, t2.id, since) == 80.0
