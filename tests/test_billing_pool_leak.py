"""Regression: build_manual_match must not leak DB connections (dogfood-lane CRITICAL).

build_manual_match's GMP/utility-bill path opened `with SessionLocal() as db:`,
let it close, then called _array_group_excess_for_sub(db, ...) AFTER the block —
handing a dead session downstream. `.execute()` on a closed session autobegins a
fresh transaction and checks out a NEW pool connection that is never returned
(the `with` won't fire again). One leak per offtaker with a share set; a tenant
with ~15+ such offtakers exhausted the pool (size 15) so the reconcile-bills and
audit-by-array screens HUNG then 500'd (`QueuePool ... timed out`).

The fix makes _array_group_excess_for_sub own its own short-lived session. This
test builds the exact leaking path and calls build_manual_match in a loop far
LONGER than the pool depth: pre-fix it raised TimeoutError partway through;
post-fix the pool returns to zero checked-out connections after every call.
"""
from __future__ import annotations

import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_pool_leak_test")

import secrets
from datetime import datetime

from api.db import SessionLocal, engine
from api.models import Tenant, Array, UtilityAccount, Bill, BillingReportSubscription
from api.billing import delivery


def _seg(items):
    return {"billSegments": [{"segmentLineItems": items}]}


def _li(code, kwh, dollars):
    return {"unitCode": code, "unitOfMeasure": "KWH", "unitCount": kwh, "dollarAmount": dollars}


def test_build_manual_match_does_not_leak_connections():
    # Path-1 (GMP utility-bill) setup that triggers _array_group_excess_for_sub:
    #   - array with a HOST gmp account that has a bill carrying kwh_sent_to_grid
    #   - a SEPARATE offtaker gmp account whose bill credits real excess (so
    #     array_kwh is not None), on the SAME array, with a share set.
    credit_raw = _seg([_li("EXCESS", 10.0, -2.10),        # $0.21/kWh credited line
                       _li("EXCESS", 5000.0, 0.0)])
    with SessionLocal() as db:
        tid = "ten_" + secrets.token_hex(4)
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Leak",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Leak Arr", region="VT"); db.add(arr); db.flush()
        # HOST account (resolved by array_id) — its bill supplies group excess.
        host = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="H" + secrets.token_hex(3))
        db.add(host); db.flush()
        db.add(Bill(tenant_id=tid, account_id=host.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 28),
                    kwh_generated=5000, kwh_sent_to_grid=5000.0,
                    solar_credit_usd=None, raw_json=_seg([_li("EXCESS", 5000.0, 0.0)])))
        # OFFTAKER account (distinct id from host) — its bill gives billable excess.
        off = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                             account_number="O" + secrets.token_hex(3))
        db.add(off); db.flush()
        db.add(Bill(tenant_id=tid, account_id=off.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 28),
                    kwh_generated=5000, kwh_sent_to_grid=5000.0,
                    solar_credit_usd=None, raw_json=credit_raw))
        db.commit()
        off_id, arr_id = off.id, arr.id

    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Leak Cust", utility_account_id=off_id,
        array_id=arr_id, allocation_pct=0.25, array_share_pct=0.25,
        billing_model="percent_of_array")

    # Far more iterations than the pool depth (Postgres default 15; SQLite-file
    # QueuePool defaults to 5+10). Pre-fix this raised TimeoutError partway.
    baseline = engine.pool.checkedout()
    for i in range(30):
        m = delivery.build_manual_match(sub)
        assert m.matched is True
        # every call must fully release — no monotonic climb toward exhaustion
        assert engine.pool.checkedout() <= baseline, (
            f"connection leak: checkedout climbed to {engine.pool.checkedout()} "
            f"(baseline {baseline}) after {i + 1} build_manual_match calls")
    # confirm the real-math path was actually exercised (the branch that leaked)
    assert m.computed_invoice.get("array_group_excess_kwh") is not None
