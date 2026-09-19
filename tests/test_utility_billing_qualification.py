"""Unqualified capture providers and estimates must never become invoice evidence."""
from datetime import date, datetime
from pathlib import Path
import pytest
from sqlalchemy import select
from api.db import SessionLocal
from api.models import Tenant, UtilityAccount, DailyGeneration, Bill, BillingReportSubscription, OfftakerInvoice
from api.billing import delivery
from tests.test_vec_offtaker_billing import _seed


def subscription(tid, aid, account, **kw):
    return BillingReportSubscription(tenant_id=tid, customer_name="Evidence test",
        utility_account_id=account, array_id=aid, allocation_pct=.4,
        net_rate_per_kwh=.25, discount_pct=.10, billing_model="percent_of_array", **kw)


@pytest.mark.parametrize("provider", ["eversource", "eversource_ma", "eversource_ct", "cmp", "unknown", ""])
def test_unqualified_provider_cannot_consume_gmp_shaped_bill(provider, monkeypatch):
    tid, aid, account = _seed(with_generation=True)
    with SessionLocal() as db:
        db.get(UtilityAccount, account).provider = provider
        db.add(Bill(tenant_id=tid, account_id=account, period_start=datetime(2026,5,1),
            period_end=datetime(2026,5,31), kwh_generated=1000,
            kwh_sent_to_grid=1000, solar_credit_usd=250))
        db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("unqualified providers must not reach GMP credit resolution")
    monkeypatch.setattr(delivery, "_utility_bill_credit", forbidden)
    match = delivery.build_match(subscription(tid, aid, account), period_label="2026-05")
    assert not match.matched and match.latest_period is None
    assert "not qualified" in " ".join(match.warnings)


@pytest.mark.parametrize("estimated_days,estimated_kwh", [(31,100), (1,1), (1,0), (31,0)])
def test_any_prorated_daily_evidence_is_held(estimated_days, estimated_kwh):
    tid, aid, account = _seed(with_generation=True)
    with SessionLocal() as db:
        rows = db.scalars(select(DailyGeneration).where(DailyGeneration.array_id == aid)
            .order_by(DailyGeneration.day)).all()
        for row in rows[-estimated_days:]:
            row.source = "bill_prorate"
            row.kwh = estimated_kwh
        db.commit()
    match = delivery.build_manual_match(subscription(tid, aid, account), period_label="2026-05")
    assert match.computed_invoice["has_utility_bill"] is False
    assert match.computed_invoice["kwh_source"] == "bill_prorate"
    assert match.computed_invoice["amount_owed"] == 0
    assert "Estimated" in " ".join(match.warnings)


@pytest.mark.parametrize("label", [None, "2026-Q2"])
def test_quarterly_generation_cannot_consume_single_month(label):
    tid, aid, account = _seed(with_generation=True)
    match = delivery.build_manual_match(subscription(tid, aid, account, cadence="quarterly"), period_label=label)
    assert match.computed_invoice["has_utility_bill"] is False
    assert match.computed_invoice["amount_owed"] == 0
    assert "quarterly evidence" in " ".join(match.warnings)


def test_monthly_actual_read_for_quarterly_trueup_stays_available():
    tid, aid, account = _seed(with_generation=True)
    match = delivery.build_manual_match(subscription(tid, aid, account, cadence="quarterly"), period_label="2026-05")
    assert match.computed_invoice["has_utility_bill"] is True
    assert match.computed_invoice["amount_owed"] == 90


def test_explicit_workbook_does_not_require_utility_adapter():
    tid, aid, account = _seed(with_generation=False)
    with SessionLocal() as db:
        db.get(UtilityAccount, account).provider = "unknown"
        db.commit()
    sub = subscription(tid, aid, account)
    sub.allocation_pct = None  # workbook evidence is the selected source
    sub.source_workbook = (Path(__file__).parent / "fixtures/billing/norwich.xlsx").read_bytes()
    match = delivery.build_match(sub)
    assert match.matched and match.latest_period is not None
    assert match.computed_invoice["amount_owed"] > 0


def test_already_frozen_history_survives_provider_reclassification():
    tid, aid, account = _seed(with_generation=True)
    sub = subscription(tid, aid, account)
    original = delivery.build_manual_match(sub, period_label="2026-05")
    with SessionLocal() as db:
        db.add(sub); db.flush()
        db.add(OfftakerInvoice(tenant_id=tid, subscription_id=sub.id,
            period_key="2026-05", period_start=date(2026,5,1), period_end=date(2026,5,31),
            invoice_number="2026-05", amount_cents=9000, credit_applied_cents=0,
            customer_kwh=400, snapshot=original.to_dict(), status="accepted"))
        db.get(UtilityAccount, account).provider = "unknown"
        db.commit()
        restored = delivery.build_match(sub, period_label="2026-05")
        assert restored.matched
        assert restored.computed_invoice["amount_owed"] == 90
        assert restored.latest_period.customer_kwh == 400


@pytest.mark.parametrize("kind", ["quarterly", "estimated"])
@pytest.mark.parametrize("action", ["draft", "send"])
def test_monthly_request_and_stale_workbook_cannot_bypass_evidence_gate(kind, action, monkeypatch):
    tid, aid, account = _seed(with_generation=True)
    sub = subscription(tid, aid, account, cadence="quarterly" if kind == "quarterly" else "monthly")
    sub.source_workbook = b"stale workbook: explicit utility account and share select manual evidence"
    def forbidden(*args, **kwargs):
        raise AssertionError("held evidence must not render or send anything")
    monkeypatch.setattr(delivery, "generate_files", forbidden)
    monkeypatch.setattr("api.notify._send_via_resend", forbidden)
    with SessionLocal() as db:
        if kind == "estimated":
            row = db.scalar(select(DailyGeneration).where(DailyGeneration.array_id == aid))
            row.source = "bill_prorate"
            row.kwh = 0
            db.commit()
        tenant = db.get(Tenant, tid)
        if action == "draft":
            result = delivery.draft_subscription(db, sub, tenant, period_label="2026-05")
        else:
            result = delivery.deliver_subscription(db, sub, tenant, period_label="2026-05")
    assert result.get("held") is True, result
    assert result["ok"] is False
