"""
VEC / SmartHub offtaker invoicing (Ford's "model A", Jun 2026).

A VEC/SmartHub utility bill carries NO EXCESS-kWh × solar-credit-$ breakdown
(unlike GMP), so a VEC offtaker can't be priced from the bill the way a GMP
offtaker is. Instead (Ford-approved "model A") a VEC offtaker is priced as:

    allocation_pct × the array's MEASURED generation × an OPERATOR-ENTERED net rate

and we REQUIRE that operator rate — we NEVER fall back to the auto schedule's
GMP/VT default (billing a real VEC customer on a fabricated rate is a hard no).

These tests drive build_manual_match(sub) directly:
  1. VEC account + array generation + an operator rate → matched + billable.
  2. VEC account + generation but NO operator rate → NOT billable, waits.
  3. VEC account linked to an array with NO generation yet → NOT billable, waits.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_vec_offtaker_test")

from datetime import date
import secrets as _secrets

from api.db import SessionLocal
from api.models import (
    Tenant, Array, UtilityAccount, DailyGeneration,
    BillingReportSubscription,
)
from api.adapters import is_smarthub_provider
from api.billing import delivery


def _seed(*, with_generation: bool, daily_kwh: float = 100.0,
          provider: str = "vec", default_net_rate=None):
    """One tenant, one array, one VEC/SmartHub account. Optionally a month of
    DailyGeneration (source='smarthub', the value the SmartHub pull writes) so we
    can prove model-A bills off MEASURED generation. `default_net_rate` sets the
    tenant-global operator rate when provided."""
    assert is_smarthub_provider(provider), f"{provider} must be a SmartHub provider"
    tid = "ten_vec_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="VEC Test",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator",
                      default_net_rate_per_kwh=default_net_rate))
        db.flush()
        arr = Array(tenant_id=tid, name="West Glover Roaring Brook", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider=provider,
                              account_number="VEC-" + _secrets.token_hex(2),
                              nickname="West Glover VEC")
        db.add(acct); db.flush()
        if with_generation:
            # A full month of metered generation (10 days × daily_kwh).
            for d in range(1, 11):
                db.add(DailyGeneration(tenant_id=tid, array_id=arr.id,
                                       day=date(2026, 5, d), kwh=daily_kwh,
                                       source="smarthub"))
        db.commit()
        return tid, arr.id, acct.id


def test_vec_offtaker_bills_measured_generation_at_operator_rate():
    """VEC account + array generation + a per-offtaker operator rate → matched,
    measured-generation source, billable, amount = gen × pct × rate × (1−disc)."""
    tid, aid, acct_id = _seed(with_generation=True, daily_kwh=100.0)
    # 10 days × 100 = 1000 kWh measured generation for the month.
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Paul Bozuwa",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.4,
        net_rate_per_kwh=0.25,        # operator-entered rate (per-offtaker)
        discount_pct=0.10,            # explicit 10% discount
        billing_model="percent_of_array",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched
    ci = m.computed_invoice
    # HONEST provenance — measured generation, NEVER 'utility_bill'.
    assert ci["kwh_source"] != "utility_bill"
    assert ci["kwh_source"].startswith("smarthub") or ci["kwh_source"] == "daily_csv"
    assert ci["has_utility_bill"] is True            # billable (rate + generation)
    assert ci["net_rate_source"] == "customer"       # the operator's own rate
    assert ci["solar_credit_usd"] is None            # no excess+credit on VEC bills
    # Measured generation = 1000 kWh; the offtaker's share = 40% = 400 kWh.
    assert ci["array_kwh"] == 1000.0
    assert m.latest_period.customer_kwh == 400.0
    # amount = 400 × 0.25 × (1 − 0.10) = 90.0
    gen, pct, rate, disc = 1000.0, 0.4, 0.25, 0.10
    expected = round(round(gen * pct, 2) * rate * (1.0 - disc), 2)
    assert ci["amount_owed"] == expected == 90.0


def test_vec_offtaker_global_rate_is_honest_source():
    """A tenant-global operator rate (no per-offtaker override) is ALSO an honest
    source — net_rate_source == 'global', and the invoice bills."""
    tid, aid, acct_id = _seed(with_generation=True, daily_kwh=50.0,
                              default_net_rate=0.20)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Global Rate Co",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=1.0, billing_model="percent_of_array",
    )
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert ci["has_utility_bill"] is True
    assert ci["net_rate_source"] == "global"
    assert ci["array_kwh"] == 500.0                  # 10 × 50


def test_vec_offtaker_waits_when_no_operator_rate():
    """VEC + generation but NO operator rate (per-offtaker None AND tenant default
    None) → NOT billable; net_rate_source 'needs_rate'; a warning tells the operator
    to set the rate. Proves we will NOT bill VEC at the auto/VT default."""
    tid, aid, acct_id = _seed(with_generation=True, daily_kwh=100.0,
                              default_net_rate=None)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="No Rate Co",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, billing_model="percent_of_array",
        send_mode="to_me", operator_email="op@e.com",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched                                  # renders for previews…
    ci = m.computed_invoice
    assert ci["has_utility_bill"] is False            # …but not billable
    assert ci["net_rate_source"] == "needs_rate"
    assert ci["net_rate_per_kwh"] == 0.0              # never the VT default
    assert any("rate" in w.lower() for w in m.warnings), m.warnings
    # And delivery SKIPS rather than emailing a default-priced invoice.
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        res = delivery.deliver_subscription(db, sub, tenant, is_test=True)
    assert res.get("skipped") is True
    assert res.get("ok") is False


def test_vec_offtaker_waits_when_no_generation_yet():
    """VEC linked to an array with NO generation yet, even with a rate set →
    NOT billable (array_kwh None); delivery skips/waits."""
    tid, aid, acct_id = _seed(with_generation=False)
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="No Gen Co",
        utility_account_id=acct_id, array_id=aid,
        allocation_pct=0.5, net_rate_per_kwh=0.25,
        billing_model="percent_of_array",
        send_mode="to_me", operator_email="op@e.com",
    )
    m = delivery.build_manual_match(sub)
    assert m.matched
    ci = m.computed_invoice
    assert ci["has_utility_bill"] is False            # no generation → waits
    assert ci["array_kwh"] == 0.0
    assert m.latest_period.customer_kwh == 0.0
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        res = delivery.deliver_subscription(db, sub, tenant, is_test=True)
    assert res.get("skipped") is True
    assert res.get("ok") is False
