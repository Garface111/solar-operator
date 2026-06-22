"""
OFFTAKER ↔ UTILITY BILL reports (Ford's rule, Jun 2026).

An offtaker bound to a GMP utility account is invoiced EXCLUSIVELY from that
account's utility PAPER BILLS (Bill.kwh_generated per period) — never vendor /
inverter telemetry, never DailyGeneration, never the GMP hourly-interval data,
and with NO fallback. If no utility bill covers a period yet, delivery SKIPS
(waits) rather than fabricating or substituting another source.

These tests prove:
  1. bound offtaker uses the BILL kWh even when (conflicting) vendor
     DailyGeneration exists for the same array → vendor data is ignored.
  2. no utility bill yet → match flags has_utility_bill False and
     deliver_subscription returns skipped (no $0 invoice sent).
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_offtaker_util_test")

from datetime import date, datetime
import secrets as _secrets

from api.db import SessionLocal
from api.models import (
    Tenant, Array, UtilityAccount, Bill, DailyGeneration,
    BillingReportSubscription,
)
from api.billing import delivery


def _seed(*, with_bill: bool, bill_kwh: int = 1800, vendor_kwh: float = 9999.0):
    """One tenant, one array, one GMP account. Optionally a utility bill on the
    account, and ALWAYS a conflicting vendor DailyGeneration so we can prove the
    bill path ignores it."""
    tid = "ten_offtk_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="Offtaker Test",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Roaring Brook", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="GMP-" + _secrets.token_hex(2),
                              nickname="Roaring Brook GMP")
        db.add(acct); db.flush()
        # Conflicting VENDOR data for the same array — must be IGNORED by the
        # utility-bill path (proves no vendor leakage).
        for d in range(1, 5):
            db.add(DailyGeneration(tenant_id=tid, array_id=arr.id,
                                   day=date(2026, 5, d), kwh=vendor_kwh / 4.0,
                                   source="solaredge"))
        if with_bill:
            db.add(Bill(tenant_id=tid, account_id=acct.id,
                        period_start=datetime(2026, 5, 1),
                        period_end=datetime(2026, 5, 31),
                        kwh_generated=bill_kwh))
        db.commit()
        return tid, arr.id, acct.id


def test_offtaker_uses_utility_bill_not_vendor():
    """Bound offtaker invoice = allocation × the UTILITY BILL kWh, ignoring the
    (much larger) vendor DailyGeneration on the same array."""
    tid, aid, acct_id = _seed(with_bill=True, bill_kwh=1800, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Paul Bozuwa",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched
    ci = m.computed_invoice
    # Source must be the utility bill, never vendor.
    assert ci["kwh_source"] == "utility_bill"
    assert ci["has_utility_bill"] is True
    # Array total = the BILL's 1800 kWh (NOT the 9999 vendor figure).
    assert ci["array_kwh"] == 1800.0
    # Customer share = 50% × 1800 = 900 kWh.
    assert m.latest_period.customer_kwh == 900.0


def test_offtaker_skips_when_no_utility_bill():
    """No utility bill yet → flagged has_utility_bill False; deliver_subscription
    SKIPS instead of sending a $0 invoice built on vendor data."""
    tid, aid, acct_id = _seed(with_bill=False, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Paul Bozuwa",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
        send_mode="to_me", operator_email="op@e.com",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched                                   # renders, but…
    assert m.computed_invoice["has_utility_bill"] is False
    assert m.computed_invoice["kwh_source"] == "utility_bill"  # never vendor
    # Delivery must SKIP (not send) because no utility bill covers the period.
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        res = delivery.deliver_subscription(db, sub, tenant, is_test=True)
    assert res.get("skipped") is True
    assert res.get("ok") is False


def test_unbound_offtaker_skips_rather_than_invoicing_telemetry():
    """GUARDRAIL: an offtaker with NO utility_account_id (never bound to a GMP
    account) must NOT be invoiced from generation telemetry. Even with healthy
    vendor DailyGeneration on the array, deliver_subscription SKIPS — the only
    valid invoice source for a typed offtaker is the GMP paper bill. This closes
    the silent 'unbound → daily_csv telemetry invoice' gap."""
    tid, aid, acct_id = _seed(with_bill=False, vendor_kwh=12000.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Unbound Co",
        utility_account_id=None, array_id=aid,   # NEVER bound to the GMP account
        allocation_pct=0.5, billing_model="percent_of_array",
        send_mode="to_me", operator_email="op@e.com",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched                                       # renders for previews…
    # …but the source is telemetry, never a utility bill.
    assert m.computed_invoice["kwh_source"] != "utility_bill"
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        res = delivery.deliver_subscription(db, sub, tenant, is_test=True)
    assert res.get("skipped") is True
    assert res.get("ok") is False
