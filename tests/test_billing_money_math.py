"""Money-adjacent billing math — table-driven lock-down (billing-tests lane).

Every dollar an offtaker is billed flows through a small set of pure/DB functions.
This file pins the EXACT rules with synthetic, anonymized fixtures (never Bruce's
real numbers) so a refactor can't silently shift a rate, mix in an estimate, or
re-normalize an allocation:

  1. CREDIT-RATE SELECTION (api/rate_schedule.excess_credit_rate_from_bill)
     — the group-net-metering case: value the shared excess at the BILL'S OWN
       stated rate (credited-line-only), NOT a fleet reference. Plus SOLCRED
       addition, the $0-shared-excess dilution trap, and the banked floor.
  2. PRORATE EXCLUSION — the invoice's measured-generation path flags a
     bill_prorate-dominated month as an ESTIMATE (never 'utility_bill'), and the
     Stripe usage query EXCLUDES bill_prorate rows from billable kWh.
  3. VEC/SmartHub manual-rate path (delivery.build_manual_match) —
     allocation_pct × MEASURED generation × an OPERATOR-ENTERED rate, and it
     REFUSES to bill on the GMP/VT default.
  4. ALLOCATION MATH — _normalized_allocations coercion/drop rules and the
     multi-array sum with percent totals OVER and UNDER 100% (faithful sum, no
     hidden normalization — a >100% roster must overbill loudly, not be capped).
  5. EXPORT ROW LAYOUTS — QuickBooks and Xero import layouts are DIFFERENT and
     must stay that way (≥1 assertion each, plus a divergence check).
"""
from __future__ import annotations

import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_money_math_test")

import secrets
from datetime import date, datetime

import pytest

from api.db import SessionLocal
from api.models import (
    Tenant, Array, UtilityAccount, Bill, DailyGeneration,
    BillingReportSubscription,
)
from api.rate_schedule import (
    excess_credit_rate_from_bill, solar_credit_from_bill,
    resolve_offtaker_excess_credit, DEFAULT_CREDIT_RATE,
    BANKED_CREDIT_RATE_FLOOR,
)
from api.billing import delivery, qb_export as q


# ─── fixtures helpers ────────────────────────────────────────────────────────

def _seg(items):
    """Wrap line items in the billSegments/segmentLineItems shape the readers expect."""
    return {"billSegments": [{"segmentLineItems": items}]}


def _li(code, kwh, dollars):
    return {"unitCode": code, "unitOfMeasure": "KWH", "unitCount": kwh, "dollarAmount": dollars}


def _tenant(db, product="array_operator", **kw):
    tid = "ten_" + secrets.token_hex(4)
    db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="MM",
                  contact_email=f"{tid}@e.com", active=True, product=product, **kw))
    db.flush()
    return tid


# ══════════════════════════════════════════════════════════════════════════════
# 1. CREDIT-RATE SELECTION — the bill's OWN net-metering rate
# ══════════════════════════════════════════════════════════════════════════════

# (label, line-items, expected_rate|None). Rates are synthetic, not real bills.
_CREDIT_RATE_CASES = [
    # Group net metering: 12 kWh credited @ -$2.40 (=$0.20/kWh) while 40,000 kWh of
    # shared excess sits on a $0 line. The rate MUST come from the credited line
    # only ($0.20), NOT be diluted across all 40,012 kWh (which would trip the
    # banked floor and force a fleet reference — the bug this reader fixes).
    ("group_net_metering_uses_credited_line_rate",
     [_li("EXCESS", 12.0, -2.40), _li("EXCESS", 40000.0, 0.0)], 0.20),
    # SOLCRED adds to the credited excess: (18 + 7) / 100 = $0.25/kWh.
    ("solcred_is_added_to_excess_credit",
     [_li("EXCESS", 100.0, -18.0), _li("SOLCRED", 100.0, -7.0)], 0.25),
    # A plain cashed excess line with no SOLCRED: 30 / 150 = $0.20/kWh.
    ("plain_excess_line",
     [_li("EXCESS", 150.0, -30.0)], 0.20),
    # EXCESSO alias is treated the same as EXCESS: 21 / 100 = $0.21.
    ("excesso_alias_counts",
     [_li("EXCESSO", 100.0, -21.0)], 0.21),
    # Truly banked: all excess on $0 lines, nothing credited → None (no rate).
    ("banked_all_zero_returns_none",
     [_li("EXCESS", 5000.0, 0.0)], None),
    # Below the banked floor: 500 kWh credited only $2 → $0.004/kWh < floor → None.
    ("below_banked_floor_returns_none",
     [_li("EXCESS", 500.0, -2.0)], None),
    # No KWH credit lines at all (only a consumption charge) → None.
    ("no_credit_lines_returns_none",
     [_li("CONSUMED", 800.0, 140.0)], None),
]


@pytest.mark.parametrize("label,items,expected", _CREDIT_RATE_CASES,
                         ids=[c[0] for c in _CREDIT_RATE_CASES])
def test_excess_credit_rate_selection(label, items, expected):
    got = excess_credit_rate_from_bill(_seg(items))
    if expected is None:
        assert got is None, f"{label}: expected banked/None, got {got}"
    else:
        assert got is not None and abs(got - expected) < 1e-4, \
            f"{label}: expected ~{expected}, got {got}"


def test_group_excess_not_diluted_by_solar_credit_from_bill():
    """The contrast that motivates excess_credit_rate_from_bill: solar_credit_from_bill
    divides the small residual credit across ALL excess kWh, diluting to ~$0 and
    tripping the banked floor → it returns None for the group case. The dedicated
    rate reader recovers the bill's real $0.20/kWh. Locking BOTH proves the fix."""
    items = [_li("EXCESS", 12.0, -2.40), _li("EXCESS", 40000.0, 0.0)]
    assert solar_credit_from_bill(_seg(items)) is None          # diluted → banked
    assert abs(excess_credit_rate_from_bill(_seg(items)) - 0.20) < 1e-4  # recovered


def test_resolver_group_bill_uses_own_rate_not_reference():
    """End-to-end through resolve_offtaker_excess_credit: a GMP group-net-metering
    bill whose solar_credit_usd was never captured (shared out at $0) still prices
    the shared excess at the BILL'S OWN stated rate, source='bill_cash' — NOT a
    fleet/reference estimate."""
    raw = _seg([_li("EXCESS", 15.0, -3.15),          # 3.15/15 = $0.21/kWh
                _li("EXCESS", 22000.0, 0.0)])
    with SessionLocal() as db:
        tid = _tenant(db)
        a = Array(tenant_id=tid, name="Group Arr", region="VT"); db.add(a); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider="gmp",
                              account_number="G" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 28),
                    kwh_generated=22000, kwh_sent_to_grid=22000.0,
                    solar_credit_usd=None, raw_json=raw))
        db.commit(); acct_id = acct.id
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, acct_id)
    assert source == "bill_cash"                     # the bill's own rate, not reference
    assert excess == 22000.0                          # bills the SHARED excess pool
    assert abs(rate - 0.21) < 1e-4
    assert credit == round(22000.0 * rate, 2)


def test_resolver_truly_banked_falls_back_to_reference():
    """When the bill genuinely credits nothing (all $0 excess lines), the resolver
    uses a REFERENCE rate — proving the bill's-own-rate path is gated on a real
    credited line, not applied blindly."""
    raw = _seg([_li("EXCESS", 3000.0, 0.0)])
    with SessionLocal() as db:
        tid = _tenant(db)
        a = Array(tenant_id=tid, name="Banked Arr", region="VT"); db.add(a); db.flush()
        # Unique provider so no fleet median exists → DEFAULT_CREDIT_RATE.
        acct = UtilityAccount(tenant_id=tid, array_id=a.id, provider="zzz_mm_" + secrets.token_hex(2),
                              account_number="B" + secrets.token_hex(3))
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 6, 1), period_end=datetime(2026, 6, 28),
                    kwh_generated=3000, kwh_sent_to_grid=3000.0,
                    solar_credit_usd=None, raw_json=raw))
        db.commit(); acct_id = acct.id
    with SessionLocal() as db:
        excess, credit, rate, ps, pe, label, source = resolve_offtaker_excess_credit(db, acct_id)
    assert source == "reference"
    assert abs(rate - DEFAULT_CREDIT_RATE) < 1e-6


# ══════════════════════════════════════════════════════════════════════════════
# 2. PRORATE EXCLUSION — an estimate is never billed as a measured/utility figure
# ══════════════════════════════════════════════════════════════════════════════

def _seed_array_with_daily(db, tid, rows):
    """rows: [(day:int, source, kwh)] in 2026-05. Returns array_id."""
    a = Array(tenant_id=tid, name="Prorate Arr", region="VT"); db.add(a); db.flush()
    for d, src, k in rows:
        db.add(DailyGeneration(tenant_id=tid, array_id=a.id, day=date(2026, 5, d),
                               kwh=k, source=src))
    db.flush()
    return a.id


def test_prorate_dominated_month_flagged_as_estimate():
    """A month whose kWh is ≥50% bill_prorate is flagged dom='bill_prorate' by
    _array_period_kwh — so downstream provenance shows an ESTIMATE, never a
    metered/'daily_csv' figure. (The value still sums for display; the FLAG is the
    honesty contract the send-guard and UI key off.)"""
    with SessionLocal() as db:
        tid = _tenant(db)
        aid = _seed_array_with_daily(db, tid, [
            (1, "bill_prorate", 100.0), (2, "bill_prorate", 100.0),
            (3, "bill_prorate", 100.0), (4, "csv", 10.0)])
        db.commit()
    with SessionLocal() as db:
        kwh, s, e, label, dom = delivery._array_period_kwh(db, aid)
    assert dom == "bill_prorate"          # estimate-dominated → flagged
    assert kwh == 310.0                   # total present (display), but flagged


def test_metered_dominated_month_flagged_daily_csv():
    """The mirror case: when real reads dominate, dom='daily_csv' (measured)."""
    with SessionLocal() as db:
        tid = _tenant(db)
        aid = _seed_array_with_daily(db, tid, [
            (1, "csv", 100.0), (2, "csv", 100.0), (3, "bill_prorate", 10.0)])
        db.commit()
    with SessionLocal() as db:
        kwh, s, e, label, dom = delivery._array_period_kwh(db, aid)
    assert dom == "daily_csv"
    assert kwh == 210.0


def test_usage_report_excludes_prorate_from_billable_kwh():
    """The Stripe metered-usage query (jobs/usage_report) that becomes a DOLLAR
    figure MUST exclude bill_prorate rows — a smeared estimate can never inflate
    what a metered AO tenant is charged."""
    from api.jobs import usage_report
    with SessionLocal() as db:
        tid = _tenant(db)
        # 200 measured + 500 prorate; only the 200 measured may be billable.
        _seed_array_with_daily(db, tid, [
            (10, "csv", 200.0), (11, "bill_prorate", 500.0)])
        db.commit()
    with SessionLocal() as db:
        total = usage_report.tenant_period_kwh(db, tid, since_date=date(2026, 5, 1))
    assert total == 200.0, f"prorate leaked into billable kWh: {total}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. VEC / SmartHub manual-rate path
# ══════════════════════════════════════════════════════════════════════════════

def _seed_vec(db, *, gen_days=10, daily_kwh=100.0, provider="vec",
              default_net_rate=None):
    from api.adapters import is_smarthub_provider
    assert is_smarthub_provider(provider)
    tid = _tenant(db, default_net_rate_per_kwh=default_net_rate)
    arr = Array(tenant_id=tid, name="VEC Arr", region="VT"); db.add(arr); db.flush()
    acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider=provider,
                          account_number="V" + secrets.token_hex(3),
                          nickname="VEC nick")
    db.add(acct); db.flush()
    for d in range(1, gen_days + 1):
        db.add(DailyGeneration(tenant_id=tid, array_id=arr.id, day=date(2026, 5, d),
                               kwh=daily_kwh, source="smarthub"))
    db.flush()
    return tid, arr.id, acct.id


# (label, pct, per_offtaker_rate, tenant_default, discount, expect_billable,
#  expected_amount|None, expected_rate_source)
_VEC_CASES = [
    # allocation × measured gen × operator rate × (1−disc):
    # 1000 kWh × 0.4 = 400; 400 × 0.25 × 0.9 = 90.00
    ("per_offtaker_rate_bills", 0.4, 0.25, None, 0.10, True, 90.00, "customer"),
    # tenant-global rate is also honest: 1000 × 1.0 × 0.20 × 1.0 = 200.00
    ("global_rate_bills", 1.0, None, 0.20, 0.0, True, 200.00, "global"),
    # NO rate anywhere → NOT billable, never the VT default, amount irrelevant.
    ("no_rate_waits", 0.5, None, None, 0.0, False, None, "needs_rate"),
]


@pytest.mark.parametrize(
    "label,pct,rate,tdefault,disc,billable,amount,rate_source", _VEC_CASES,
    ids=[c[0] for c in _VEC_CASES])
def test_vec_manual_rate_path(label, pct, rate, tdefault, disc, billable,
                              amount, rate_source):
    with SessionLocal() as db:
        tid, aid, acct_id = _seed_vec(db, gen_days=10, daily_kwh=100.0,
                                      default_net_rate=tdefault)
        db.commit()
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="VEC Cust", utility_account_id=acct_id,
        array_id=aid, allocation_pct=pct, net_rate_per_kwh=rate,
        discount_pct=disc, billing_model="percent_of_array")
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert ci["kwh_source"] != "utility_bill"          # measured, never a bill
    assert ci["has_utility_bill"] is billable
    assert ci["net_rate_source"] == rate_source
    assert ci["solar_credit_usd"] is None              # VEC bills carry no credit
    if billable:
        assert ci["array_kwh"] == 1000.0
        assert ci["amount_owed"] == amount
    else:
        assert ci["net_rate_per_kwh"] == 0.0           # NEVER the auto/VT default


# ══════════════════════════════════════════════════════════════════════════════
# 4. ALLOCATION MATH — coercion, drop rules, and percent-sum edges (over/under 100)
# ══════════════════════════════════════════════════════════════════════════════

class _Alloc:
    def __init__(self, allocations):
        self.array_allocations = allocations


# (label, raw_allocations, expected_clean)
_NORMALIZE_CASES = [
    ("coerces_string_types",
     [{"array_id": "7", "allocation_pct": "0.6"}],
     [{"array_id": 7, "allocation_pct": 0.6}]),
    ("drops_zero_and_negative_and_null_id",
     [{"array_id": 1, "allocation_pct": 0.5}, {"array_id": 2, "allocation_pct": 0.0},
      {"array_id": 3, "allocation_pct": -0.3}, {"array_id": None, "allocation_pct": 0.4}],
     [{"array_id": 1, "allocation_pct": 0.5}]),
    ("empty_returns_empty", [], []),
    # OVER 100%: two arrays at 0.6+0.7=1.3 — kept faithfully, NOT normalized to 1.0.
    ("over_100pct_not_normalized",
     [{"array_id": 1, "allocation_pct": 0.6}, {"array_id": 2, "allocation_pct": 0.7}],
     [{"array_id": 1, "allocation_pct": 0.6}, {"array_id": 2, "allocation_pct": 0.7}]),
    # UNDER 100%: 0.25+0.25=0.5 — also faithful (partial ownership is legitimate).
    ("under_100pct_kept",
     [{"array_id": 1, "allocation_pct": 0.25}, {"array_id": 2, "allocation_pct": 0.25}],
     [{"array_id": 1, "allocation_pct": 0.25}, {"array_id": 2, "allocation_pct": 0.25}]),
]


@pytest.mark.parametrize("label,raw,expected", _NORMALIZE_CASES,
                         ids=[c[0] for c in _NORMALIZE_CASES])
def test_normalized_allocations(label, raw, expected):
    assert delivery._normalized_allocations(_Alloc(raw)) == expected


def test_normalized_allocations_never_raises_on_garbage():
    """Total function: malformed input yields [] (fall back to legacy path), never
    an exception into the invoice."""
    assert delivery._normalized_allocations(_Alloc(None)) == []
    assert delivery._normalized_allocations(_Alloc("not a list")) == []
    assert delivery._normalized_allocations(_Alloc([{"array_id": "x"}])) == []
    class _NoField: pass
    assert delivery._normalized_allocations(_NoField()) == []


def _seed_multi_array(db, allocations):
    """Two arrays, each with a full month of measured generation, and a sub whose
    array_allocations spans them. allocations: [(kwh, pct)]. Returns (sub, arr_ids)."""
    tid = _tenant(db)
    aids = []
    raw_alloc = []
    for kwh, pct in allocations:
        arr = Array(tenant_id=tid, name=f"MA {secrets.token_hex(2)}", region="VT")
        db.add(arr); db.flush()
        # One metered day carrying the whole month's kWh (simplest exact fixture).
        db.add(DailyGeneration(tenant_id=tid, array_id=arr.id, day=date(2026, 5, 15),
                               kwh=kwh, source="csv"))
        aids.append(arr.id)
        raw_alloc.append({"array_id": arr.id, "allocation_pct": pct})
    db.flush()
    sub = BillingReportSubscription(
        tenant_id=tid, customer_name="Multi Cust",
        array_allocations=raw_alloc, net_rate_per_kwh=0.20, discount_pct=0.0,
        billing_model="percent_of_array")
    return sub, aids


def test_multi_array_sum_over_100pct_bills_faithfully():
    """The money-critical edge: a roster that sums to >100% must produce an invoice
    that reflects the literal sum (overbilling loudly), not a silently capped 100%.
    Arrays: 1000 kWh @ 0.6 + 2000 kWh @ 0.7 = 600 + 1400 = 2000 customer kWh."""
    with SessionLocal() as db:
        sub, aids = _seed_multi_array(db, [(1000.0, 0.6), (2000.0, 0.7)])
        db.commit()
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert m.latest_period.customer_kwh == 2000.0     # 600 + 1400, NOT capped
    assert len(ci["array_breakdown"]) == 2
    # amount = 2000 × 0.20 × 1.0 = 400.00
    assert ci["amount_owed"] == 400.00


def test_multi_array_sum_under_100pct_bills_faithfully():
    """Partial ownership across arrays: 1000 @ 0.25 + 400 @ 0.5 = 250 + 200 = 450."""
    with SessionLocal() as db:
        sub, aids = _seed_multi_array(db, [(1000.0, 0.25), (400.0, 0.5)])
        db.commit()
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert m.latest_period.customer_kwh == 450.0
    assert ci["amount_owed"] == round(450.0 * 0.20, 2) == 90.0


# ══════════════════════════════════════════════════════════════════════════════
# 5. EXPORT ROW LAYOUTS — QuickBooks vs Xero must differ, and stay pinned
# ══════════════════════════════════════════════════════════════════════════════

_INV = {"customer_name": "Muni Co", "invoice_number": "2026-06",
        "invoice_date": "2026-06-30", "due_date": "2026-07-28",
        "month": "2026-06", "amount_owed": 1234.56}


class _ExpSub:
    client_email = "ap@muni.example"


def test_export_layouts_are_different():
    """QuickBooks and Xero import layouts are DIFFERENT files — different headers,
    different column order, different arity. Lock the divergence so a refactor can't
    collapse them into one and break an operator's bookkeeping import."""
    assert q.QB_HEADER != q.XERO_HEADER
    assert q.QB_HEADER[0] == "InvoiceNo" and q.XERO_HEADER[0] == "ContactName"
    # Xero carries EmailAddress + AccountCode/TaxType; QB carries Item + amounts.
    assert "EmailAddress" in q.XERO_HEADER and "EmailAddress" not in q.QB_HEADER
    assert "Item(Product/Service)" in q.QB_HEADER and "Item(Product/Service)" not in q.XERO_HEADER
    assert len(q.QB_HEADER) == 9 and len(q.XERO_HEADER) == 10


def test_xero_row_exact():
    f = q._invoice_fields(_INV, _ExpSub())
    row = q._xero_row(f, account_code="200", tax_type="Tax Exempt")
    assert row[0] == "Muni Co"                    # ContactName
    assert row[1] == "ap@muni.example"            # EmailAddress (Xero-only)
    assert row[2] == "2026-06"                    # InvoiceNumber
    assert row[3] == "6/30/2026"                  # InvoiceDate M/D/YYYY
    assert row[6] == 1 and row[7] == 1234.56      # Quantity, UnitAmount
    assert row[8] == "200" and row[9] == "Tax Exempt"
    assert len(row) == len(q.XERO_HEADER)


def test_quickbooks_row_exact():
    f = q._invoice_fields(_INV, _ExpSub())
    row = q._qb_row(f, item_name="Solar Credit")
    assert row[0] == "2026-06"                    # InvoiceNo
    assert row[1] == "Muni Co"                    # Customer
    assert row[4] == "Solar Credit"               # Item(Product/Service) (QB-only)
    # QB repeats amount as both ItemRate and ItemAmount (qty is always 1).
    assert row[6] == 1 and row[7] == 1234.56 and row[8] == 1234.56
    assert len(row) == len(q.QB_HEADER)


def test_export_never_emits_zero_or_missing_amount():
    """Neither layout emits a fabricated $0 row — money exports only real invoices."""
    assert q._invoice_fields({"customer_name": "Z", "amount_owed": 0}, _ExpSub()) is None
    assert q._invoice_fields({"customer_name": "Z", "amount_owed": None}, _ExpSub()) is None


def test_mdY_us_date_no_leading_zeros():
    """Both exports use US M/D/YYYY with no leading zeros — lock the format both
    QuickBooks and Xero import parsers expect."""
    assert q._mdY("2026-06-05") == "6/5/2026"
    assert q._mdY("2026-12-30") == "12/30/2026"
    assert q._mdY(date(2026, 1, 9)) == "1/9/2026"
    assert q._mdY(None) == ""
