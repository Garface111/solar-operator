"""Real-math offtaker invoicing (api/billing/delivery build_manual_match).

Ford/Anna: the invoice should bill the offtaker's SHARE of the array's group
excess (the correct allocation), not whatever GMP credited on their own bill
(which GMP sometimes gets wrong) — but ONLY when array_share_pct + the array's
group excess are both present and the offtaker has a separate account (a real
cross-check). Otherwise fall back to today's GMP-credited figure so no invoice
silently changes without the data to back it. Real sends stay gated regardless.
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
CREDITED = 7343.0     # what GMP credited on the offtaker's own bill (2 low)


def _seed(share=0.2553, single_meter=False, host_bill=True):
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
                        kwh_sent_to_grid=GROUP, is_net_metered=True))
        if single_meter:
            off = host
        else:
            off = UtilityAccount(tenant_id=tid, provider="gmp", account_number="OFF",
                                 nickname="St. J Muni")
            db.add(off); db.flush()
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


def _patch_resolver(monkeypatch, off_acct_id):
    import api.rate_schedule as _rs
    def fake(db, acct_id, label=None):
        if acct_id == off_acct_id:
            return (CREDITED, round(CREDITED * RATE, 2), RATE, None, None, "2026-06", "bill_cash")
        return None
    monkeypatch.setattr(_rs, "resolve_offtaker_excess_credit", fake)


def _match(tid):
    from api.billing.delivery import build_match
    with SessionLocal() as db:
        sub = db.execute(
            __import__("sqlalchemy").select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == tid)).scalars().first()
        return build_match(sub).computed_invoice


def test_real_math_bills_share_of_group_not_gmp_credited(monkeypatch):
    tid, off_id = _seed(share=0.2553)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "real_math"
    assert ci["gmp_credited_kwh"] == 7343.0                 # GMP's (wrong) number kept
    assert abs(ci["realmath_kwh"] - 7345.5) < 0.3           # 0.2553 × 28,772
    assert abs(ci["kwh"] - 7345.5) < 0.3                    # BILLED on the real math
    # amount owed follows the billed (real-math) kWh at the effective rate — and is
    # HIGHER than billing GMP's short 7,343 would have been (the corrected math).
    assert abs(ci["amount_owed"] - ci["kwh"] * ci["effective_rate_per_kwh"]) < 0.5
    assert ci["amount_owed"] > 7343.0 * ci["effective_rate_per_kwh"]


def test_falls_back_to_gmp_credited_without_share(monkeypatch):
    tid, off_id = _seed(share=None)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 7343.0) < 0.1                    # own excess × pct(1.0)
    assert ci["realmath_kwh"] is None


def test_single_meter_does_not_switch_basis(monkeypatch):
    # offtaker billed on the array's own meter → share × host would double-count
    tid, off_id = _seed(share=0.2553, single_meter=True)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 7343.0) < 0.1


def test_no_host_bill_falls_back(monkeypatch):
    tid, off_id = _seed(share=0.2553, host_bill=False)
    _patch_resolver(monkeypatch, off_id)
    ci = _match(tid)
    assert ci["billing_basis"] == "gmp_credited"
    assert abs(ci["kwh"] - 7343.0) < 0.1


# ─── displayed pair consistency (backlog 2026-07-01, dogfood) ────────────────
# preview-math used to return allocation_pct=1.0 beside a customer_kwh that was
# 99.4% of the displayed array total — the % was the sub's own-bill allocation
# while the kWh was share × the GROUP excess (two different bases). The
# displayed (share, base, billed-kWh) triple must live on ONE basis, with both
# raw figures kept for the side-by-side audit.

def _full_match(tid):
    from api.billing.delivery import build_match
    with SessionLocal() as db:
        sub = db.execute(
            __import__("sqlalchemy").select(BillingReportSubscription)
            .where(BillingReportSubscription.tenant_id == tid)).scalars().first()
        return sub.id, build_match(sub)


def test_real_math_displayed_pair_is_consistent(monkeypatch):
    tid, off_id = _seed(share=0.2553)
    _patch_resolver(monkeypatch, off_id)
    _, m = _full_match(tid)
    ci = m.computed_invoice
    assert ci["billing_basis"] == "real_math"
    # The pair actually billed: SHARE × the array's GROUP excess.
    assert m.allocation_pct == 0.2553
    assert ci["project_total_kwh"] == GROUP
    assert ci["array_kwh"] == GROUP
    assert abs(m.allocation_pct * ci["project_total_kwh"] - ci["kwh"]) < 0.01
    # Both raw figures preserved, clearly named, for the audit.
    assert ci["own_bill_excess_kwh"] == CREDITED
    assert ci["gmp_credited_kwh"] == CREDITED          # 1.0 × own-bill excess


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
    (allocation_pct, array_total_kwh, customer_kwh) plus the labeled basis."""
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
    assert p["billing_basis"] == "real_math"
    assert p["allocation_pct"] == 0.2553
    assert p["array_total_kwh"] == GROUP
    assert abs(p["allocation_pct"] * p["array_total_kwh"] - p["customer_kwh"]) < 0.01
    assert p["own_bill_excess_kwh"] == CREDITED
    assert p["month"] == "2026-06"
    assert p["billing_cadence"] == "monthly"
