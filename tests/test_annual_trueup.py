"""Annual budget true-up: charge underpayment or bank credit for next bill."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.billing.trueup import (
    apply_pending_credit,
    compute_annual_trueup,
    trueup_window,
    build_trueup_match,
)


def test_trueup_window_sept_covers_prior_12_months():
    start, end, labels = trueup_window(date(2026, 9, 1))
    assert start == date(2025, 9, 1)
    assert end == date(2026, 8, 31)
    assert labels[0] == "2025-09"
    assert labels[-1] == "2026-08"
    assert len(labels) == 12


def test_apply_pending_credit_partial_and_full():
    due, applied, rem = apply_pending_credit(100.0, 40.0)
    assert due == 60.0 and applied == 40.0 and rem == 0.0
    due, applied, rem = apply_pending_credit(30.0, 100.0)
    assert due == 0.0 and applied == 30.0 and rem == 70.0
    due, applied, rem = apply_pending_credit(50.0, 0.0)
    assert due == 50.0 and applied == 0.0 and rem == 0.0


def _fake_match(actual: float, budget: float = 100.0):
    return SimpleNamespace(
        matched=True,
        computed_invoice={
            "amount_owed": budget,
            "solar_credit_value": actual,
            "budget_override": True,
            "has_utility_bill": True,
            "kwh_source": "utility_bill",
        },
    )


def test_compute_annual_trueup_charge_when_actual_exceeds_budget():
    sub = SimpleNamespace(
        customer_name="Acme",
        budget_amount_usd=100.0,
        annual_trueup=True,
        last_trueup_window_end=None,
        source_workbook=None,
        utility_account_id=1,
        allocation_pct=0.1,
        client_email="a@b.com",
    )
    # 12 months at $120 actual vs $100 budget → $240 underpaid
    with patch("api.billing.trueup._period_figures") as fig:
        def _row(sub, lab):
            from api.billing.trueup import MonthTrueup
            return MonthTrueup(period_label=lab, budgeted_usd=100.0,
                               actual_usd=120.0, included=True)
        fig.side_effect = _row
        s = compute_annual_trueup(sub, as_of=date(2026, 9, 1))
    assert s.ok
    assert s.months_included == 12
    assert s.total_budgeted == 1200.0
    assert s.total_actual == 1440.0
    assert s.charge_usd == 240.0
    assert s.credit_usd == 0.0


def test_compute_annual_trueup_credit_when_overpaid():
    sub = SimpleNamespace(
        customer_name="Acme",
        budget_amount_usd=100.0,
        annual_trueup=True,
        last_trueup_window_end=None,
        source_workbook=None,
        utility_account_id=1,
        allocation_pct=0.1,
        client_email="a@b.com",
    )
    with patch("api.billing.trueup._period_figures") as fig:
        def _row(sub, lab):
            from api.billing.trueup import MonthTrueup
            return MonthTrueup(period_label=lab, budgeted_usd=100.0,
                               actual_usd=80.0, included=True)
        fig.side_effect = _row
        s = compute_annual_trueup(sub, as_of=date(2026, 9, 1))
    assert s.ok
    assert s.charge_usd == 0.0
    assert s.credit_usd == 240.0  # 12 * 20


def test_compute_requires_budget():
    sub = SimpleNamespace(
        customer_name="Acme", budget_amount_usd=None,
        last_trueup_window_end=None, annual_trueup=True,
    )
    s = compute_annual_trueup(sub, as_of=date(2026, 9, 1))
    assert not s.ok
    assert "budget" in (s.error or "").lower()


def test_idempotent_when_window_already_settled():
    sub = SimpleNamespace(
        customer_name="Acme", budget_amount_usd=100.0,
        last_trueup_window_end=date(2026, 8, 31), annual_trueup=True,
    )
    s = compute_annual_trueup(sub, as_of=date(2026, 9, 1))
    assert not s.ok
    assert "already" in (s.error or "").lower()


def test_build_trueup_match_charge_and_credit_notes():
    from api.billing.trueup import TrueupSettlement
    sub = SimpleNamespace(
        customer_name="Acme Co", allocation_pct=0.25, client_email="a@b.com",
    )
    charge = TrueupSettlement(
        ok=True, window_start=date(2025, 9, 1), window_end=date(2026, 8, 31),
        total_budgeted=1200, total_actual=1500, difference=300,
        charge_usd=300, credit_usd=0, months_included=12,
    )
    m = build_trueup_match(sub, charge)
    assert m.matched
    assert m.computed_invoice["is_trueup"] is True
    assert m.computed_invoice["amount_owed"] == 300.0
    assert "exceeded" in (m.computed_invoice.get("trueup_note") or "").lower()

    credit = TrueupSettlement(
        ok=True, window_start=date(2025, 9, 1), window_end=date(2026, 8, 31),
        total_budgeted=1200, total_actual=1000, difference=-200,
        charge_usd=0, credit_usd=200, months_included=12,
    )
    m2 = build_trueup_match(sub, credit)
    assert m2.computed_invoice["amount_owed"] == 0.0
    assert m2.computed_invoice["trueup_credit_usd"] == 200
    assert "credit" in (m2.computed_invoice.get("trueup_note") or "").lower()


def test_build_match_applies_pending_credit(client):
    """Integration: banked credit reduces budgeted amount_owed."""
    # Lightweight unit of apply_pending_credit already covered; verify the
    # delivery build_match path stamps credit_applied when pending_credit > 0.
    from api.billing.delivery import build_match
    from api.db import SessionLocal
    from api.models import Tenant, Array, BillingReportSubscription
    import secrets

    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="TU", contact_email=f"{tid}@t.test",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.flush()
        arr = Array(tenant_id=tid, name="A", latitude=44.0, longitude=-72.0)
        db.add(arr)
        db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, customer_name="Budget Customer",
            array_id=arr.id, allocation_pct=1.0,
            budget_amount_usd=200.0, pending_credit_usd=50.0,
            annual_trueup=True, cadence="monthly", enabled=True,
            send_mode="to_me", source_workbook=None,
        )
        db.add(sub)
        db.commit()
        sub_id = sub.id

    # Without a utility bill, match may not be fully billable — stub build path.
    fake_ci = {
        "amount_owed": 150.0,  # pre-budget computed
        "has_utility_bill": True,
        "kwh_source": "utility_bill",
        "kwh": 1000,
        "period_end": "2026-07-31",
        "period_start": "2026-07-01",
    }
    fake = SimpleNamespace(
        matched=True,
        latest_period=SimpleNamespace(end=date(2026, 7, 31)),
        computed_invoice=fake_ci,
        allocation_pct=1.0,
        customer={"name": "Budget Customer"},
        warnings=[],
    )

    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sub_id)
        with patch("api.billing.delivery.build_manual_match", return_value=fake):
            m = build_match(sub)
    ci = m.computed_invoice
    # Budget 200, credit 50 → due 150
    assert ci["budget_override"] is True
    assert ci["amount_before_credit"] == 200.0
    assert ci["credit_applied"] == 50.0
    assert ci["amount_owed"] == 150.0
