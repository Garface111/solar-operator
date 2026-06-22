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


def _seed(*, with_bill: bool, bill_excess: float = 1800.0,
          bill_credit_rate: float = 0.2576, vendor_kwh: float = 9999.0,
          bill_kwh: int = 1800, provider: str = "gmp"):
    """One tenant, one array, one GMP account. Optionally a utility bill carrying
    the offtaker billing basis — EXCESS kWh sent to grid (kwh_sent_to_grid) + the
    gross solar credit (solar_credit_usd = excess × the credit rate) — and ALWAYS a
    conflicting vendor DailyGeneration so we can prove the bill path ignores it."""
    tid = "ten_offtk_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="Offtaker Test",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Roaring Brook", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider=provider,
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
                        kwh_generated=bill_kwh,
                        kwh_sent_to_grid=bill_excess,
                        solar_credit_usd=round(bill_excess * bill_credit_rate, 2)))
        db.commit()
        return tid, arr.id, acct.id


def test_offtaker_uses_utility_bill_not_vendor():
    """Bound offtaker invoice = allocation × the bill's EXCESS sent to grid, valued
    at the bill's actual net-metering credit rate (EXCESS+SOLCRED) — ignoring the
    (much larger) vendor DailyGeneration on the same array."""
    tid, aid, acct_id = _seed(with_bill=True, bill_excess=1800.0,
                              bill_credit_rate=0.2576, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Paul Bozuwa",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched
    ci = m.computed_invoice
    # Source is the utility bill's solar credit, never vendor.
    assert ci["kwh_source"] == "utility_bill"
    assert ci["has_utility_bill"] is True
    assert ci["net_rate_source"] == "gmp_bill_credit"
    # Basis = the bill's EXCESS (1800 kWh), NOT the 9999 vendor figure.
    assert ci["array_kwh"] == 1800.0
    assert ci["excess_kwh"] == 1800.0
    # Credit rate = the bill's actual rate; gross credit = 1800 × 0.2576 = 463.68.
    assert abs(ci["net_rate_per_kwh"] - 0.2576) < 1e-6
    assert ci["solar_credit_usd"] == 463.68
    # Customer share = 50% × 1800 = 900 kWh excess.
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


def test_offtaker_banked_month_bills_at_reference_rate():
    """Option B (Ford, 2026-06-22): a BANKED month — big EXCESS sent to grid but
    solar_credit_usd NULL (credited at ~$0, rolled forward, not cashed) — is NOT
    skipped and NOT over-charged from gross kWh × a flat rate. The offtaker is
    billed for the EXCESS at a REFERENCE credit rate (here DEFAULT_CREDIT_RATE, with
    no fleet/history available), so a perpetual-banker like Londonderry bills
    monthly for the solar received instead of $0-until-annual-true-up."""
    from api.rate_schedule import DEFAULT_CREDIT_RATE
    tid, aid, acct_id = _seed(with_bill=False, vendor_kwh=9999.0,
                              provider="zzz_banked_ref")     # unique → no fleet
    with SessionLocal() as db:
        db.add(Bill(tenant_id=tid, account_id=acct_id,
                    period_start=datetime(2026, 5, 1),
                    period_end=datetime(2026, 5, 31),
                    kwh_generated=56400, kwh_sent_to_grid=56320.0,
                    solar_credit_usd=None))            # banked → reference rate
        db.commit()
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Londonderry",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=1.0, billing_model="percent_of_array",
    )
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert ci["has_utility_bill"] is True                    # bills (not skip)
    assert ci["net_rate_source"] == "gmp_credit_reference"   # banked → reference
    assert ci["excess_kwh"] == 56320.0                       # bills the excess
    assert abs(ci["net_rate_per_kwh"] - DEFAULT_CREDIT_RATE) < 1e-6
    assert m.latest_period.customer_kwh == 56320.0           # 100% of excess
