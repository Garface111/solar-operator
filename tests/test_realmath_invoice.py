"""Offtaker billing basis (api/billing/delivery build_manual_match).

Ford 2026-07-10 (REVERSAL of the 2026-07-01 real_math-wins rule): the offtaker's
OWN utility bill governs the invoice — its excess is GMP's actual allocation of
the net-meter group. The operator-entered share (array_share_pct) exists for the
BILL-ACCURACY AUDIT (entered vs GMP-derived share), not to move the invoice:
editing the share must never change a bill that has its own settled utility bill
behind it. Entered share × the group's HOST pool (real_math) bills ONLY as the
fallback when the offtaker's own sub-account has no settled bill for the period.
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_realmath_test")

from datetime import datetime
import secrets

from api.db import SessionLocal
from api.models import (Tenant, Array, UtilityAccount, Bill, Client,
                        BillingReportSubscription)

RATE = 0.16
GROUP = 28772.0
CREDITED = 7343.0     # what GMP credited on the offtaker's own bill


def _seed(share=0.2553, single_meter=False, host_bill=True, own_bill=True):
    tid = "ten_rm_" + secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name="RM",
                      contact_email=f"{tid}@e.com", active=True, product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Timberworks", region="VT"); db.add(arr); db.flush()
        host = UtilityAccount(tenant_id=tid, provider="gmp", account_number="HOST",
                              array_id=arr.id)
        db.add(host); db.flush()
        if host_bill:
            db.add(Bill(tenant_id=tid, account_id=host.id, period_start=datetime(2026, 6, 1),
                        period_end=datetime(2026, 6, 30), kwh_generated=28788,
                        kwh_sent_to_grid=GROUP, solar_credit_usd=round(GROUP * RATE, 2),
                        is_net_metered=True))
        if single_meter:
            off = host
        else:
            off = UtilityAccount(tenant_id=tid, provider="gmp", account_number="OFF",
                                 nickname="St. J Muni", array_id=arr.id)
            db.add(off); db.flush()
        if own_bill:
            db.add(Bill(tenant_id=tid, account_id=off.id, period_start=datetime(2026, 6, 1),
                        period_end=datetime(2026, 6, 30), kwh_sent_to_grid=CREDITED,
                        kwh_generated=int(CREDITED), solar_credit_usd=round(CREDITED * RATE, 2),
                        is_net_metered=True))
        c = Client(tenant_id=tid, name="St. J Muni", active=True); db.add(c); db.flush()
        db.add(BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name="St. J Muni", array_id=arr.id,
            allocation_pct=1.0, array_share_pct=share, utility_account_id=off.id,
            billing_model="percent_of_array", cadence="monthly", enabled=True))
        db.commit()
        return tid, off.id


def _patch_resolver(monkeypatch, off_acct_id, own_bill=True):
    """Fake the bill-credit resolver: the offtaker's own account returns its
    credited excess (or None when own_bill=False — no settled sub bill), any
    OTHER account (the host, used by the no-own-bill fallback) returns the
    group pool at the bill's rate."""
    import api.rate_schedule as _rs
    def fake(db, acct_id, label=None):
        if acct_id == off_acct_id:
            if not own_bill:
                return None
            return (CREDITED, round(CREDITED * RATE, 2), RATE, None, None, "2026-06", "bill_cash")
        return (GROUP, round(GROUP * RATE, 2), RATE,
                datetime(2026, 6, 1), datetime(2026, 6, 30), "2026-06", "bill_cash")
    monkeypatch.setattr(_rs, "resolve_offtaker_excess_credit", fake)


def _match(tid):
    from api.billing.delivery import build_match
    with SessionLocal() as db:
        sub = db.execute(
            __import__("sqlalchemy").select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == tid)).scalars().first()
        return build_match(sub).computed_invoice


def test_own_bill_governs_even_with_share(monkeypatch):
    """The core reversal: a settled sub bill BILLS, the entered share does not."""
    tid, off_id = _seed(share=0.2553)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - CREDITED) < 0.1                  # BILLED off their own bill
    assert ci["gmp_credited_kwh"] == CREDITED
    # The entered-share figure is KEPT as the audit side-figure, not billed.
    assert abs(ci["realmath_kwh"] - 7345.5) < 0.3           # 0.2553 × 28,772
    # GMP's derived share — the % "pulled from the bill".
    assert abs(ci["derived_share_pct"] - CREDITED / GROUP) < 1e-6
    assert abs(ci["amount_owed"] - ci["kwh"] * ci["effective_rate_per_kwh"]) < 0.5


def test_share_edit_never_moves_a_billed_invoice(monkeypatch):
    """Editing the audit share must not change the amount when an own bill exists."""
    tid, off_id = _seed(share=0.2553)
    _patch_resolver(monkeypatch, off_id)
    before = _match(tid)["amount_owed"]
    with SessionLocal() as db:
        sub = db.execute(
            __import__("sqlalchemy").select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == tid)).scalars().first()
        sub.array_share_pct = 0.5
        db.commit()
    after = _match(tid)
    assert abs(after["amount_owed"] - before) < 0.01
    assert after["billing_basis"] == "gmp_credited"


def test_no_own_bill_falls_back_to_entered_share(monkeypatch):
    """Ford's fallback: no settled sub bill → entered share × the HOST pool at
    the host bill's rate; switches to the own bill automatically once it lands."""
    tid, off_id = _seed(share=0.2553, own_bill=False)
    _patch_resolver(monkeypatch, off_id, own_bill=False)
    from api.billing.delivery import build_match
    with SessionLocal() as db:
        sub = db.execute(
            __import__("sqlalchemy").select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == tid)).scalars().first()
        m = build_match(sub)
    ci = m.computed_invoice
    assert ci["billing_basis"] == "real_math"
    assert abs(ci["kwh"] - 0.2553 * GROUP) < 0.3
    assert ci["own_bill_excess_kwh"] is None                # honest: no own bill
    assert ci["gmp_credited_kwh"] is None                   # never renders as "0 credited"
    assert ci["array_kwh"] == GROUP                         # displayed pair = share × pool
    assert m.allocation_pct == 0.2553
    assert any("own sub-account" in w for w in m.warnings)


def test_falls_back_to_gmp_credited_without_share(monkeypatch):
    tid, off_id = _seed(share=None)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 7343.0) < 0.1                    # own excess × pct(1.0)
    assert ci["realmath_kwh"] is None


def test_single_meter_does_not_switch_basis(monkeypatch):
    # offtaker billed on the array's own meter → gmp_credited, share never bills
    tid, off_id = _seed(share=0.2553, single_meter=True)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 7343.0) < 0.1


def test_no_host_bill_still_bills_own_bill(monkeypatch):
    tid, off_id = _seed(share=0.2553, host_bill=False)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 7343.0) < 0.1


# ─── displayed pair consistency (backlog 2026-07-01, dogfood) ────────────────
# The displayed (share, base, billed-kWh) triple must live on ONE basis, with
# both raw figures kept for the side-by-side audit.

def _full_match(tid):
    from api.billing.delivery import build_match
    with SessionLocal() as db:
        sub = db.execute(
            __import__("sqlalchemy").select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == tid)).scalars().first()
        return sub.id, build_match(sub)


def test_own_bill_displayed_pair_is_consistent(monkeypatch):
    tid, off_id = _seed(share=0.2553)
    _patch_resolver(monkeypatch, off_id)
    _, m = _full_match(tid)
    ci = m.computed_invoice
    assert ci["billing_basis"] == "gmp_credited"
    # The pair actually billed: pct(1.0) × their OWN bill's excess.
    assert m.allocation_pct == 1.0
    assert ci["project_total_kwh"] == CREDITED
    assert ci["array_kwh"] == CREDITED
    assert abs(m.allocation_pct * ci["project_total_kwh"] - ci["kwh"]) < 0.01
    # Both raw audit figures preserved, clearly named.
    assert ci["own_bill_excess_kwh"] == CREDITED
    assert ci["array_group_excess_kwh"] == GROUP
    assert abs(ci["realmath_kwh"] - 0.2553 * GROUP) < 0.3


def test_gmp_credited_displayed_pair_unchanged(monkeypatch):
    """Without a share the basis stays gmp_credited and the displayed pair is
    the classic one: own-bill excess × allocation_pct."""
    tid, off_id = _seed(share=None)
    _patch_resolver(monkeypatch, off_id)
    _, m = _full_match(tid)
    ci = m.computed_invoice
    assert m.allocation_pct == 1.0
    assert ci["project_total_kwh"] == CREDITED
    assert abs(m.allocation_pct * ci["project_total_kwh"] - ci["kwh"]) < 0.01


def test_preview_math_endpoint_returns_consistent_pair(client, monkeypatch):
    """Route-level regression: /preview-math must return a self-consistent
    (allocation_pct, array_total_kwh, customer_kwh) plus the labeled basis —
    on the OWN-BILL basis now that it governs."""
    from api.account import mint_session_for_tenant
    tid, off_id = _seed(share=0.2553)
    _patch_resolver(monkeypatch, off_id)
    sub_id, _ = _full_match(tid)
    auth = f"Bearer {mint_session_for_tenant(tid)}"
    r = client.get(
        f"/v1/array-operator/billing/subscriptions/{sub_id}/preview-math",
        headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["has_data"] is True
    assert p["billing_basis"] == "gmp_credited"
    assert p["allocation_pct"] == 1.0
    assert p["array_total_kwh"] == CREDITED
    assert abs(p["allocation_pct"] * p["array_total_kwh"] - p["customer_kwh"]) < 0.01
    assert p["own_bill_excess_kwh"] == CREDITED
    assert p["month"] == "2026-06"
    assert p["billing_cadence"] == "monthly"
