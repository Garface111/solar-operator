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
        csv_text, count = build_invoice_register(db, DEMO_TENANT_ID)  # Xero default
        assert count > 0, "expected real billable invoices in the export"
        assert csv_text.splitlines()[0].startswith("ContactName")
        qb_text, qb_count = build_invoice_register(db, DEMO_TENANT_ID, fmt="quickbooks")
        assert qb_count == count
        assert qb_text.splitlines()[0].startswith("InvoiceNo")


def test_review_emails_batched_one_digest_per_operator():
    """An operator with many offtakers gets ONE 'come review' digest, not one
    email per offtaker (Ford's 100-emails complaint). Also proves the seeded
    offtaker bills are now SETTLED (kwh_generated set) — the review sweep only
    sees them if they are, so a non-zero count confirms the visibility fix."""
    from api.jobs.new_bill_review import run_new_bill_reviews
    seed_realistic_demo(arrays=2, offtakers_per_array=3)   # 6 offtakers, one operator
    res = run_new_bill_reviews(dry_run=True)
    demo = [p for p in res.get("previews", []) if p["tenant_id"] == DEMO_TENANT_ID]
    assert len(demo) == 1, f"expected ONE digest for the demo operator, got {len(demo)}"
    p = demo[0]
    assert p["count"] == 6, p["count"]            # all 6 offtakers in one email
    assert "6 solar invoices are ready" in p["subject"], p["subject"]
    assert p["items"][0]["amount_usd"] is not None  # bills settled → real amount


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
