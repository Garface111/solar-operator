"""Invoice ↔ GMP-bill reconciliation (api/billing/reconcile_bills.py).

Proves the comparison: when a GMP bill is linked to an array, the report
compares our invoice's produced-kWh against the bill's kwh_generated and emits
match / mismatch; when no bill is linked, it honestly says no_bill (never fakes).
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_recon_test")

from datetime import date, datetime
import secrets

from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, DailyGeneration,
                        BillingReportSubscription, Client)
from api.billing import reconcile_bills


def _seed():
    tid = "ten_recon_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Recon",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        # Array A: has a GMP bill that MATCHES our production (1000 kWh June).
        a1 = Array(tenant_id=tid, name="Maple Grove", region="VT")
        # Array B: has a GMP bill that MISMATCHES (bill says 800, we have 1000).
        a2 = Array(tenant_id=tid, name="Fair Haven", region="VT")
        # Array C: NO GMP bill linked.
        a3 = Array(tenant_id=tid, name="Stratton Ridge", region="VT")
        db.add_all([a1, a2, a3]); db.flush()
        for a in (a1, a2, a3):
            for d in range(1, 5):
                db.add(DailyGeneration(tenant_id=tid, array_id=a.id, day=date(2026, 6, d),
                                       kwh=250.0, source="csv"))   # 1000 kWh each, June
        # GMP accounts + bills for A1 (match) and A2 (mismatch).
        acc1 = UtilityAccount(tenant_id=tid, provider="gmp", account_number="111", array_id=a1.id)
        acc2 = UtilityAccount(tenant_id=tid, provider="gmp", account_number="222", array_id=a2.id)
        db.add_all([acc1, acc2]); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acc1.id, period_start=datetime(2026, 6, 1),
                    period_end=datetime(2026, 6, 30), kwh_generated=1000, total_cost=210.0))
        db.add(Bill(tenant_id=tid, account_id=acc2.id, period_start=datetime(2026, 6, 1),
                    period_end=datetime(2026, 6, 30), kwh_generated=800, total_cost=168.0))
        # Subscriptions (manual, single-array each).
        for arr, pct in ((a1, 1.0), (a2, 1.0), (a3, 1.0)):
            c = Client(tenant_id=tid, name=f"Off {arr.name}", active=True); db.add(c); db.flush()
            db.add(BillingReportSubscription(
                tenant_id=tid, client_id=c.id, customer_name=f"Off {arr.name}",
                array_id=arr.id, allocation_pct=pct, billing_model="percent_of_array",
                cadence="monthly"))
        db.commit()
        return tid, a1.id, a2.id, a3.id


def test_reconcile_match_mismatch_and_no_bill():
    tid, a1, a2, a3 = _seed()
    with SessionLocal() as db:
        rep = reconcile_bills.reconcile_tenant(db, tid)
    assert rep["ok"] and rep["subscription_count"] == 3
    by_array = {}
    for s in rep["subscriptions"]:
        for r in s["arrays"]:
            by_array[r["array_id"]] = r
    # A1: our 1000 vs GMP 1000 → match
    assert by_array[a1]["status"] == "match", by_array[a1]
    assert by_array[a1]["gmp_kwh"] == 1000.0
    # A2: our 1000 vs GMP 800 → mismatch, +200 delta
    assert by_array[a2]["status"] == "mismatch", by_array[a2]
    assert abs(by_array[a2]["delta_kwh"] - 200.0) < 0.5
    # A3: no GMP bill → no_bill, never fabricated
    assert by_array[a3]["status"] == "no_bill", by_array[a3]
    assert by_array[a3]["gmp_kwh"] is None


def test_allocation_cross_check_catches_gmp_error(monkeypatch):
    """Anna/Bruce's $25 catch: GMP credits an offtaker an excess that doesn't
    match their share of the array's group excess. Array bill says the group
    excess is 28,772; at a 25.53% share GMP should credit ~7,345 kWh, but the
    offtaker's own bill shows 7,343 → implied group total 28,762, a number on
    neither bill. reconcile flags an allocation mismatch (never a fake match)."""
    tid = "ten_alloc_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Alloc",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Timberworks", region="VT"); db.add(arr); db.flush()
        # The ARRAY's own GMP account + bill carrying the group excess sent to grid.
        acc_arr = UtilityAccount(tenant_id=tid, provider="gmp", account_number="ARR",
                                 array_id=arr.id)
        # The OFFTAKER's OWN GMP account (distinct meter → real allocation to check).
        acc_off = UtilityAccount(tenant_id=tid, provider="gmp", account_number="OFF")
        db.add_all([acc_arr, acc_off]); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acc_arr.id, period_start=datetime(2026, 6, 1),
                    period_end=datetime(2026, 6, 30), kwh_generated=28788,
                    kwh_sent_to_grid=28772.0, total_cost=0.0))
        c = Client(tenant_id=tid, name="St. J Muni", active=True); db.add(c); db.flush()
        db.add(BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="St. J Muni",
            array_id=arr.id, allocation_pct=0.2553, utility_account_id=acc_off.id,
            billing_model="percent_of_array", cadence="monthly"))
        db.commit()
        aid = arr.id

    # GMP's actual credit on the offtaker's own bill (7,343 kWh) — patch the shared
    # resolver so we assert the allocation math, not the bill-parsing pipeline.
    import api.rate_schedule as _rs
    monkeypatch.setattr(_rs, "resolve_offtaker_excess_credit",
                        lambda db, acct_id, label=None: (7343.0, 1351.0, 0.18398,
                                                         None, None, "2026-06", "bill_cash"))

    with SessionLocal() as db:
        rep = reconcile_bills.reconcile_tenant(db, tid)
    sub = rep["subscriptions"][0]
    alloc = sub["allocation"]
    assert alloc["status"] == "mismatch", alloc
    assert alloc["offtaker_credited_kwh"] == 7343.0
    assert abs(alloc["expected_kwh"] - 7345.5) < 0.3, alloc
    assert abs(alloc["implied_group_total_kwh"] - 28762.2) < 0.5, alloc
    assert rep["allocation_flagged"] == 1, rep["allocation_counts"]
