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


def test_operator_entered_rate_overrides_the_bill_credit_rate():
    """Ford 2026-06-28: the solar credit rate DEFAULTS to the bill's net-metering
    credit rate, but if the operator TYPES a rate it OVERRIDES the bill. Same bill
    (rate 0.2576) → with sub.net_rate_per_kwh=0.25 the invoice prices the excess at
    0.25, not 0.2576, and flags the source as the operator's rate. (Blank → bill
    rate is proven by test_offtaker_uses_utility_bill_not_vendor above.)"""
    tid, aid, acct_id = _seed(with_bill=True, bill_excess=1800.0,
                              bill_credit_rate=0.2576, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Valley Cares, Inc.",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
        net_rate_per_kwh=0.25,          # operator-entered override
        discount_pct=0.0,               # isolate the rate (no discount)
    )
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert ci["kwh_source"] == "utility_bill"
    assert ci["net_rate_source"] == "customer"           # entered rate wins
    assert abs(ci["net_rate_per_kwh"] - 0.25) < 1e-6     # 0.25, NOT the bill's 0.2576


def test_explicit_bill_and_share_override_stored_workbook():
    """Ford 2026-06-28: a SPREADSHEET (workbook) offtaker that ALSO has a linked GMP
    bill + a share set must bill PERCENT-OF-ARRAY from the GMP bill — the explicit
    bill+share config overrides the stored source_workbook (which would otherwise
    re-parse the uploaded sheet). The workbook bytes here are deliberately INVALID:
    if path-selection wrongly took the workbook branch it would raise, so this proves
    bill+share force the manual (bill-driven) path and the sheet is never parsed."""
    tid, aid, acct_id = _seed(with_bill=True, bill_excess=1800.0,
                              bill_credit_rate=0.2576, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Valley Cares, Inc.",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
        source_workbook=b"NOT-A-REAL-XLSX",        # would raise if wrongly parsed
    )
    m = delivery.build_match(sub)
    assert m is not None and m.matched
    ci = m.computed_invoice
    assert ci["kwh_source"] == "utility_bill"       # billed from the GMP bill, not the sheet
    assert ci["excess_kwh"] == 1800.0
    assert m.latest_period.customer_kwh == 900.0     # 50% of the bill's 1800 kWh excess


def test_budget_bill_overrides_on_workbook_offtaker_bill_path():
    """The full Valley Cares case: a workbook offtaker with bill+share AND a custom
    budget bill. It bills from the GMP bill (workbook ignored), but the fixed budget
    total overrides the computed amount — exactly how budget billing already works.
    The real computed solar-credit value is preserved for display."""
    tid, aid, acct_id = _seed(with_bill=True, bill_excess=1800.0,
                              bill_credit_rate=0.2576, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Valley Cares, Inc.",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
        source_workbook=b"NOT-A-REAL-XLSX",
        budget_amount_usd=2150.0,
    )
    m = delivery.build_match(sub)
    ci = m.computed_invoice
    assert ci["kwh_source"] == "utility_bill"        # still bill-driven under the hood
    assert ci["budget_override"] is True
    assert ci["amount_owed"] == 2150.0               # the budget total wins
    assert ci.get("solar_credit_value") is not None  # real computed credit preserved


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


# ── Editable Solar-credit-rate override + honest default (Ford 2026-07-07) ─────
# The rate field DEFAULTS to the bill's rate but the operator can OVERRIDE it. The
# computed invoice must always carry the bill-derived DEFAULT (value + honest
# source) so the UI can show "default: $X — <bill | banked reference>" even while
# an override is in force. See rateFieldHTML / paintRateField in array-operator.

def test_default_net_rate_exposes_bill_rate_when_cashed():
    """A cashed month with NO override: the invoice prices at the bill rate AND
    exposes default_net_rate_* == the bill's own rate/source, so the editable
    field can label it 'from your GMP bill' honestly."""
    tid, aid, acct_id = _seed(with_bill=True, bill_excess=1800.0,
                              bill_credit_rate=0.2576, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="No Override Co",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
    )
    ci = delivery.build_manual_match(sub).computed_invoice
    assert ci["net_rate_source"] == "gmp_bill_credit"
    assert abs(ci["net_rate_per_kwh"] - 0.2576) < 1e-6
    # The honest DEFAULT mirrors the bill's own rate + source (no override set).
    assert ci["default_net_rate_source"] == "gmp_bill_credit"
    assert abs(ci["default_net_rate_per_kwh"] - 0.2576) < 1e-6
    assert "GMP bill" in (ci["default_net_rate_note"] or "") \
        or "net-metering credit" in (ci["default_net_rate_note"] or "")


def test_override_wins_but_default_still_reports_the_bill_rate():
    """Override set: the invoice AMOUNT recomputes at the override rate and
    net_rate_source flips to 'customer' — but default_net_rate_* STILL reports
    the underlying bill rate so the UI shows 'default: $0.2576 — from your GMP
    bill' as the fallback beneath the operator's override."""
    tid, aid, acct_id = _seed(with_bill=True, bill_excess=1800.0,
                              bill_credit_rate=0.2576, vendor_kwh=9999.0)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Override Co",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
        net_rate_per_kwh=0.30,          # operator override, ABOVE the bill rate
        discount_pct=0.0,               # isolate the rate
    )
    ci = delivery.build_manual_match(sub).computed_invoice
    # Override wins for the actual invoice.
    assert ci["net_rate_source"] == "customer"
    assert abs(ci["net_rate_per_kwh"] - 0.30) < 1e-6
    # Amount recomputed at the override: 900 kWh × 0.30 = 270.00.
    assert abs(ci["amount_owed"] - 270.0) < 1e-6
    # …but the DEFAULT still honestly reports the bill's own rate/source.
    assert ci["default_net_rate_source"] == "gmp_bill_credit"
    assert abs(ci["default_net_rate_per_kwh"] - 0.2576) < 1e-6


def test_default_net_rate_reports_reference_when_banked():
    """A BANKED month (solar_credit_usd None): default_net_rate_source must be
    'gmp_credit_reference' — so the UI labels it a comparable-months reference,
    NOT 'from your GMP bill'. This is the Town of Fairlee prod case."""
    from api.rate_schedule import DEFAULT_CREDIT_RATE
    tid, aid, acct_id = _seed(with_bill=False, vendor_kwh=9999.0,
                              provider="zzz_banked_def")     # unique → no fleet
    with SessionLocal() as db:
        db.add(Bill(tenant_id=tid, account_id=acct_id,
                    period_start=datetime(2026, 5, 1),
                    period_end=datetime(2026, 5, 31),
                    kwh_generated=56400, kwh_sent_to_grid=56320.0,
                    solar_credit_usd=None))                  # banked
        db.commit()
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Town of Fairlee",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=1.0, billing_model="percent_of_array",
    )
    ci = delivery.build_manual_match(sub).computed_invoice
    assert ci["net_rate_source"] == "gmp_credit_reference"
    # The DEFAULT honestly reflects the banked-reference source (NOT the bill).
    assert ci["default_net_rate_source"] == "gmp_credit_reference"
    assert abs(ci["default_net_rate_per_kwh"] - DEFAULT_CREDIT_RATE) < 1e-6
    assert "banked" in (ci["default_net_rate_note"] or "")


def test_override_on_banked_month_prices_at_override():
    """Town of Fairlee's real need: a BANKED month whose reference rate the
    operator wants to correct. Setting net_rate_per_kwh prices the excess at the
    override, and the default still reports the banked reference underneath."""
    tid, aid, acct_id = _seed(with_bill=False, vendor_kwh=9999.0,
                              provider="zzz_banked_ovr")
    with SessionLocal() as db:
        db.add(Bill(tenant_id=tid, account_id=acct_id,
                    period_start=datetime(2026, 5, 1),
                    period_end=datetime(2026, 5, 31),
                    kwh_generated=1000, kwh_sent_to_grid=1000.0,
                    solar_credit_usd=None))                  # banked
        db.commit()
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Town of Fairlee",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=1.0, billing_model="percent_of_array",
        net_rate_per_kwh=0.18, discount_pct=0.0,
    )
    ci = delivery.build_manual_match(sub).computed_invoice
    assert ci["net_rate_source"] == "customer"
    assert abs(ci["net_rate_per_kwh"] - 0.18) < 1e-6
    assert abs(ci["amount_owed"] - 180.0) < 1e-6            # 1000 × 0.18
    assert ci["default_net_rate_source"] == "gmp_credit_reference"
