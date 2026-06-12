"""Demo seed is idempotent: running it twice yields the same data state."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount, Bill
from scripts.seed_demo_tenant import seed, DEMO_TENANT_ID

# Pin the clock so generated bills are identical run-to-run regardless of when
# the test executes.
FIXED_TODAY = date(2026, 6, 6)


def _snapshot() -> dict:
    """Canonical, timestamp-free fingerprint of the demo data."""
    with SessionLocal() as db:
        t = db.get(Tenant, DEMO_TENANT_ID)
        clients = sorted(
            c.name for c in db.execute(
                select(Client).where(Client.tenant_id == DEMO_TENANT_ID)
            ).scalars()
        )
        arrays = sorted(
            (a.name, a.nepool_gis_id) for a in db.execute(
                select(Array).where(Array.tenant_id == DEMO_TENANT_ID)
            ).scalars()
        )
        accts = sorted(
            a.account_number for a in db.execute(
                select(UtilityAccount).where(UtilityAccount.tenant_id == DEMO_TENANT_ID)
            ).scalars()
        )
        bills = sorted(
            (b.document_number, b.kwh_generated) for b in db.execute(
                select(Bill).where(Bill.tenant_id == DEMO_TENANT_ID)
            ).scalars()
        )
        return {
            "tenant": (t.name, t.is_demo, t.plan, t.subscription_status, t.tenant_key),
            "clients": clients,
            "arrays": arrays,
            "accounts": accts,
            "bills": bills,
        }


def test_seed_is_idempotent(client):
    seed(today=FIXED_TODAY)
    first = _snapshot()
    seed(today=FIXED_TODAY)
    second = _snapshot()
    assert first == second


def test_seed_shape_is_realistic(client):
    seed(today=FIXED_TODAY)
    snap = _snapshot()
    # ~15 clients (seed sized to show off the operator's range — see
    # scripts/seed_demo_tenant.py), each NEPOOL id is 5 digits starting 99,
    # accounts 10 digits starting 99, no future bills, every bill has
    # positive generation.
    assert 12 <= len(snap["clients"]) <= 20
    assert snap["tenant"][1] is True  # is_demo
    for _name, nepool in snap["arrays"]:
        assert nepool.startswith("99") and len(nepool) == 5
    for acct in snap["accounts"]:
        assert acct.startswith("99") and len(acct) == 10
    assert all(kwh > 0 for _doc, kwh in snap["bills"])

    with SessionLocal() as db:
        latest = max(
            b.bill_date for b in db.execute(
                select(Bill).where(Bill.tenant_id == DEMO_TENANT_ID)
            ).scalars()
        )
    # No bill dated in the future relative to the seed clock.
    assert latest.date() < FIXED_TODAY


def test_seed_no_real_stripe_identity(client):
    seed(today=FIXED_TODAY)
    with SessionLocal() as db:
        t = db.get(Tenant, DEMO_TENANT_ID)
        assert t.stripe_customer_id is None
        assert t.stripe_subscription_id is None
        assert t.plan == "demo"
