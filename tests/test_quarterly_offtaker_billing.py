"""QUARTERLY cadence for utility-bill offtakers (backlog #6, built 2026-07-02).

A quarterly-cadence offtaker invoice must aggregate the FULL quarter — the sum
of all three monthly settled utility bills' EXCESS + credit — never bill just
one of the three months (the old behavior, blocked with a 400 until this
feature existed). Honesty rules under test:

  • quarter sum is exact: kWh = Σ month excess × share; dollars reproduce each
    month's own credit rate (blended rate = Σ credit ÷ Σ excess);
  • a quarter MISSING a settled month is HELD (has_utility_bill False →
    delivery skips) with the missing month named — never silently under-billed;
  • months before the account's FIRST-EVER bill aren't "missing": a service
    started mid-quarter bills the covered months with the range clearly marked;
  • real-math (share × group excess) sums the HOST bills over the same months,
    and falls back to gmp_credited when the host pool isn't fully covered;
  • monthly cadence is completely untouched;
  • the create/PATCH 400 guard is LIFTED for GMP offtakers (still blocked for
    VEC/SmartHub model-A, which prices single months of measured generation).
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_quarterly_test")

from datetime import datetime
import secrets

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, Client,
                        BillingReportSubscription)
from api.billing import delivery

# Q2 2026 offtaker bills: (month, last_day, excess_kwh, credit_rate)
Q2 = [(4, 30, 1000.0, 0.16), (5, 31, 1200.0, 0.17), (6, 30, 800.0, 0.18)]
Q2_EXCESS = 3000.0
Q2_CREDIT = 1000.0 * 0.16 + 1200.0 * 0.17 + 800.0 * 0.18       # 508.00


def _bill(db, tid, acct_id, m, last_day, excess, rate):
    db.add(Bill(tenant_id=tid, account_id=acct_id,
                period_start=datetime(2026, m, 1),
                period_end=datetime(2026, m, last_day),
                kwh_generated=int(excess * 1.3),
                kwh_sent_to_grid=excess,
                solar_credit_usd=round(excess * rate, 2),
                is_net_metered=True))


def _seed(months, *, host_months=None, share=None, cadence="quarterly",
          pct=0.5):
    """One tenant/array; an OFFTAKER GMP account with `months` bills; optionally
    a HOST account (carrying the group excess) with `host_months` bills."""
    tid = "ten_q_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Q Test",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Quarter Farm", region="VT")
        db.add(arr); db.flush()
        off = UtilityAccount(tenant_id=tid, provider="gmp",
                             account_number="OFF" + secrets.token_hex(2),
                             nickname="Quarter Farm offtaker")
        db.add(off); db.flush()
        for m, last, ex, rate in months:
            _bill(db, tid, off.id, m, last, ex, rate)
        if host_months is not None:
            host = UtilityAccount(tenant_id=tid, provider="gmp",
                                  account_number="HOST" + secrets.token_hex(2),
                                  array_id=arr.id)
            db.add(host); db.flush()
            for m, last, ex, rate in host_months:
                _bill(db, tid, host.id, m, last, ex, rate)
        c = Client(tenant_id=tid, name="Quarterly Off", active=True)
        db.add(c); db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="Quarterly Off",
            array_id=arr.id, utility_account_id=off.id,
            allocation_pct=pct, array_share_pct=share,
            billing_model="percent_of_array", cadence=cadence, enabled=True,
            send_mode="to_me", operator_email="op@e.com")
        db.add(sub); db.commit()
        return tid, sub.id


def _match(sub_id):
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        return delivery.build_match(sub)


# ─── quarter sum ─────────────────────────────────────────────────────────────

def test_quarter_sums_all_three_months():
    tid, sid = _seed(Q2)
    m = _match(sid)
    ci = m.computed_invoice
    assert ci["billing_cadence"] == "quarterly"
    assert ci["month"] == "2026-Q2"
    assert ci["invoice_number"] == "2026-Q2"
    assert ci["has_utility_bill"] is True
    assert ci["period_months"] == ["2026-04", "2026-05", "2026-06"]
    assert ci["period_missing_months"] == []
    # kWh = pct × Σ excess (never a single month).
    assert abs(ci["kwh"] - 0.5 * Q2_EXCESS) < 0.01
    assert ci["project_total_kwh"] == Q2_EXCESS
    # Dollars reproduce each month's OWN rate: blended = Σ credit / Σ excess,
    # so amount = pct × Σ credit × (1 − 10% default discount).
    assert abs(ci["amount_owed"] - 0.5 * Q2_CREDIT * 0.9) < 0.05
    assert ci["solar_credit_usd"] == round(Q2_CREDIT, 2)
    assert abs(ci["net_rate_per_kwh"] - Q2_CREDIT / Q2_EXCESS) < 1e-5
    # Covered range spans the whole quarter, and the note marks it.
    assert ci["period_start"] == "2026-04-01"
    assert ci["period_end"] == "2026-06-30"
    assert "2026-Q2" in (ci["period_note"] or "")
    # Displayed pair stays consistent: share × total == billed kWh.
    assert abs((m.allocation_pct or 0) * ci["project_total_kwh"] - ci["kwh"]) < 0.01


def test_quarter_anchors_on_latest_excess_bill_quarter():
    """A July bill moves the anchor to Q3 — which is then held (Aug/Sep bills
    haven't landed), never partially billed."""
    tid, sid = _seed(Q2 + [(7, 31, 900.0, 0.18)])
    ci = _match(sid).computed_invoice
    assert ci["month"] == "2026-Q3"
    assert ci["has_utility_bill"] is False
    assert ci["period_missing_months"] == ["2026-08", "2026-09"]


# ─── missing month → HOLD, never under-bill ─────────────────────────────────

def test_missing_month_holds_the_invoice():
    tid, sid = _seed([Q2[0], Q2[2]])            # Apr + Jun, May missing
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        m = delivery.build_match(sub)
        ci = m.computed_invoice
        assert ci["has_utility_bill"] is False   # held — nothing billable yet
        assert ci["kwh"] == 0.0
        assert ci["period_missing_months"] == ["2026-05"]
        assert any("May 2026" in w for w in m.warnings), m.warnings
        # Delivery must SKIP (hold), not send 2 of 3 months.
        tenant = db.get(Tenant, tid)
        res = delivery.deliver_subscription(db, sub, tenant, is_test=True)
    assert res.get("ok") is False
    assert res.get("skipped") is True


# ─── mid-quarter service start → bill covered range, clearly marked ─────────

def test_partial_service_start_bills_covered_range():
    tid, sid = _seed([Q2[1], Q2[2]])            # first-ever bill = May
    ci = _match(sid).computed_invoice
    assert ci["has_utility_bill"] is True
    assert ci["period_months"] == ["2026-05", "2026-06"]
    assert abs(ci["kwh"] - 0.5 * (1200.0 + 800.0)) < 0.01
    assert ci["period_start"] == "2026-05-01"
    assert ci["period_end"] == "2026-06-30"
    assert "mid-quarter" in (ci["period_note"] or ""), ci["period_note"]


# ─── monthly cadence untouched ───────────────────────────────────────────────

def test_monthly_cadence_still_bills_latest_single_month():
    tid, sid = _seed(Q2, cadence="monthly")
    ci = _match(sid).computed_invoice
    assert ci["billing_cadence"] == "monthly"
    assert ci["month"] == "2026-06"
    assert abs(ci["kwh"] - 0.5 * 800.0) < 0.01
    assert abs(ci["amount_owed"] - 0.5 * 800.0 * 0.18 * 0.9) < 0.05


def test_month_targeted_backfill_stays_single_month():
    """Sheet-tracker backfill passes a YYYY-MM label — that must stay one
    month's figures even on a quarterly sub (the tracker appends month rows)."""
    tid, sid = _seed(Q2)
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        ci = delivery.build_match(sub, period_label="2026-05").computed_invoice
    assert ci["month"] == "2026-05"
    assert abs(ci["kwh"] - 0.5 * 1200.0) < 0.01


# ─── real math over the quarter ──────────────────────────────────────────────

def test_quarterly_real_math_sums_host_group_excess():
    host = [(4, 30, 2000.0, 0.16), (5, 31, 2400.0, 0.17), (6, 30, 1600.0, 0.18)]
    tid, sid = _seed(Q2, host_months=host, share=0.2553, pct=1.0)
    m = _match(sid)
    ci = m.computed_invoice
    assert ci["billing_basis"] == "real_math"
    assert ci["array_group_excess_kwh"] == 6000.0
    assert abs(ci["kwh"] - round(0.2553 * 6000.0, 2)) < 0.01
    # The displayed pair uses the REAL-MATH base (group pool × share) — while
    # GMP's own-bill figure is kept alongside for the audit.
    assert m.allocation_pct == 0.2553
    assert ci["project_total_kwh"] == 6000.0
    assert ci["gmp_credited_kwh"] == Q2_EXCESS        # 1.0 × own-bill quarter sum
    assert ci["own_bill_excess_kwh"] == Q2_EXCESS


def test_quarterly_real_math_falls_back_when_host_pool_incomplete():
    host = [(5, 31, 2400.0, 0.17), (6, 30, 1600.0, 0.18)]   # April host bill missing
    tid, sid = _seed(Q2, host_months=host, share=0.2553, pct=1.0)
    ci = _match(sid).computed_invoice
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - Q2_EXCESS) < 0.01          # pct(1.0) × own-bill sum


# ─── exactly-once per quarter ────────────────────────────────────────────────

def test_quarter_sends_once_then_blocks_duplicate(monkeypatch):
    tid, sid = _seed(Q2)
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        sub.client_email = "cust@e.com"
        sub.send_mode = "to_client"
        tenant = db.get(Tenant, tid)
        res1 = delivery.deliver_subscription(db, sub, tenant, is_test=False)
        assert res1["ok"] is True, res1
        assert sub.last_sent_period_end == "2026-06-30"
        res2 = delivery.deliver_subscription(db, sub, tenant, is_test=False)
    assert res2.get("already_sent") is True


# ─── the 400 guard is lifted (GMP) / kept (SmartHub model-A) ────────────────

def _route_tenant():
    tid = "ten_qr_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="Q Routes",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def test_create_and_patch_quarterly_gmp_offtaker_unblocked(client):
    tid, auth = _route_tenant()
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Route Farm", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, provider="gmp",
                              account_number="RQ1", array_id=arr.id)
        db.add(acct); db.flush()
        _bill(db, tid, acct.id, 6, 30, 500.0, 0.18)
        db.commit()
        acct_id = acct.id
    r = client.post("/v1/array-operator/billing/subscriptions",
                    data={"customer_name": "Quarterly Roue",
                          "utility_account_id": str(acct_id),
                          "allocation_pct": "0.5", "cadence": "quarterly"},
                    headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["cadence"] == "quarterly"
    # PATCH monthly → quarterly is unblocked too.
    r2 = client.patch(f"/v1/array-operator/billing/subscriptions/{sub['id']}",
                      json={"cadence": "monthly"}, headers={"Authorization": auth})
    assert r2.status_code == 200, r2.text
    r3 = client.patch(f"/v1/array-operator/billing/subscriptions/{sub['id']}",
                      json={"cadence": "quarterly"}, headers={"Authorization": auth})
    assert r3.status_code == 200, r3.text
    assert r3.json()["subscription"]["cadence"] == "quarterly"


def test_smarthub_model_a_offtaker_still_blocks_quarterly(client):
    tid, auth = _route_tenant()
    with SessionLocal() as db:
        acct = UtilityAccount(tenant_id=tid, provider="vec",
                              account_number="VQ1")
        db.add(acct); db.commit()
        acct_id = acct.id
    r = client.post("/v1/array-operator/billing/subscriptions",
                    data={"customer_name": "VEC Quarterly",
                          "utility_account_id": str(acct_id),
                          "allocation_pct": "0.5", "cadence": "quarterly"},
                    headers={"Authorization": auth})
    assert r.status_code == 400, r.text
    assert "VEC/SmartHub" in r.json()["detail"]
    # Monthly still fine; then the PATCH to quarterly is what's blocked.
    r2 = client.post("/v1/array-operator/billing/subscriptions",
                     data={"customer_name": "VEC Monthly",
                           "utility_account_id": str(acct_id),
                           "allocation_pct": "0.5", "cadence": "monthly"},
                     headers={"Authorization": auth})
    assert r2.status_code == 200, r2.text
    sid = r2.json()["subscription"]["id"]
    r3 = client.patch(f"/v1/array-operator/billing/subscriptions/{sid}",
                      json={"cadence": "quarterly"}, headers={"Authorization": auth})
    assert r3.status_code == 400, r3.text
