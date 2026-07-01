"""Realistic demo seed (api/seed_demo.py) — proves it makes the offtaker pipeline
testable end to end: real invoices, a firing bill-accuracy check (rigged errors
caught), a populated archive, and a non-empty QB/Xero export. This is the "real
data to test with" Ford asked for, verified with the real reconcile/export code."""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_seed_test")

from api.db import SessionLocal
from api.models import Tenant
from api.seed_demo import seed_realistic_demo, DEMO_TENANT_ID, DEMO_PASSWORD
from api.account import _verify_password
from api.billing import reconcile_bills
from api.billing.invoice_archive import list_archive
from api.billing.qb_export import build_invoice_register


def test_seed_lights_up_the_whole_pipeline():
    summary = seed_realistic_demo(arrays=3, offtakers_per_array=4)
    assert summary["ok"]
    assert summary["offtakers"] == 12
    assert summary["rigged_allocation_errors"] >= 1

    # Login works (Ford can sign in with the returned creds).
    with SessionLocal() as db:
        t = db.get(Tenant, DEMO_TENANT_ID)
        assert t is not None and t.product == "array_operator"
        assert _verify_password(DEMO_PASSWORD, t.password_hash)

        # Accuracy check FIRES on the rigged allocations (real reconcile code).
        rep = reconcile_bills.reconcile_tenant(db, DEMO_TENANT_ID)
        assert rep["allocation_flagged"] >= 1, rep["allocation_counts"]
        assert rep["allocation_flagged"] == summary["rigged_allocation_errors"]
        # And clean allocations are reported as matches, not false flags.
        assert rep["allocation_counts"].get("match", 0) >= 1

        # Archive is populated for the seeded period.
        arch = list_archive(db, DEMO_TENANT_ID)
        assert arch["month_count"] >= 1
        assert arch["months"][0]["invoice_count"] > 0

        # QB/Xero export has real invoice rows (header + data lines).
        csv_text, count = build_invoice_register(db, DEMO_TENANT_ID)
        assert count > 0, "expected real billable invoices in the export"
        assert csv_text.splitlines()[0].startswith("Customer")


def test_seed_is_idempotent():
    a = seed_realistic_demo(arrays=2, offtakers_per_array=2)
    b = seed_realistic_demo(arrays=2, offtakers_per_array=2)
    assert a["offtakers"] == b["offtakers"] == 4
    with SessionLocal() as db:
        # Exactly one demo tenant, not duplicated.
        from sqlalchemy import select, func
        n = db.execute(select(func.count()).select_from(Tenant)
                       .where(Tenant.id == DEMO_TENANT_ID)).scalar()
        assert n == 1
