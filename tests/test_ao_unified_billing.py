"""Unified AO billing model + Energy Agent freemium budget."""
from __future__ import annotations

from types import SimpleNamespace

from api.pricing_ao_unified import (
    AI_FREE_WEEKLY_BUDGET_USD,
    AI_PRO_MONTHLY_USD,
    ai_budget_cap_usd,
    build_unified_bill,
    tenant_has_ai_pro,
)


def test_free_weekly_sample_default():
    assert abs(AI_FREE_WEEKLY_BUDGET_USD - 2.5) < 0.01
    assert abs(AI_PRO_MONTHLY_USD - 50.0) < 0.01


def test_ai_pro_unlimited_cap():
    free = SimpleNamespace(ai_pro=False, plan="standard", is_demo=False)
    pro = SimpleNamespace(ai_pro=True, plan="standard", is_demo=False)
    comped = SimpleNamespace(ai_pro=False, plan="comped", is_demo=False)
    assert tenant_has_ai_pro(free) is False
    assert tenant_has_ai_pro(pro) is True
    assert tenant_has_ai_pro(comped) is True
    assert ai_budget_cap_usd(free) == AI_FREE_WEEKLY_BUDGET_USD
    assert ai_budget_cap_usd(pro) is None


def test_unified_bill_both_lines_plus_ai_sample():
    bill = build_unified_bill(
        billing_plan="both",
        nameplate_kw=100,
        offtaker_count=4,
        ai_pro=False,
    )
    ids = [ln["id"] for ln in bill["lines"]]
    assert "monitoring" in ids
    assert "invoicing" in ids
    assert "ai_pro" in ids
    assert "collection_fee" in ids
    ai = next(ln for ln in bill["lines"] if ln["id"] == "ai_pro")
    assert ai["included"] is False
    assert ai["amount_cents"] == 0
    # 100 kW × $0.15 = $15 = 1500¢; 4 offtakers × $15 = $60 = 6000¢
    inv = next(ln for ln in bill["lines"] if ln["id"] == "invoicing")
    assert inv["amount_cents"] == 6000
    assert inv["full_unit_cents"] == 1500
    # Collection fee is transparency-only — not in monthly total
    cf = next(ln for ln in bill["lines"] if ln["id"] == "collection_fee")
    assert cf["amount_cents"] is None
    assert cf.get("included_in_monthly_total") is False
    assert abs(float(cf["fee_percent"]) - 0.5) < 0.001
    assert bill["total_cents"] == 1500 + 6000  # mon + inv, no AI, no skim
    assert bill["collection_fee"]["fee_bps"] == 50
    assert bill["ai"]["pro"] is False
    assert bill["ai"]["free_weekly_usd"] == AI_FREE_WEEKLY_BUDGET_USD


def test_unified_bill_ai_pro_adds_fifty():
    free = build_unified_bill(
        billing_plan="monitoring", nameplate_kw=10, offtaker_count=0, ai_pro=False,
    )
    pro = build_unified_bill(
        billing_plan="monitoring", nameplate_kw=10, offtaker_count=0, ai_pro=True,
    )
    assert pro["total_cents"] >= free["total_cents"] + 4900  # ~$50
    ai = next(ln for ln in pro["lines"] if ln["id"] == "ai_pro")
    assert ai["included"] is True
    assert ai["amount_cents"] == 5000


def test_invoicing_only_shows_monitoring_unbilled():
    """Account always surfaces both product lines; only active plan lines are charged."""
    bill = build_unified_bill(
        billing_plan="invoicing",
        nameplate_kw=500,
        offtaker_count=2,
        ai_pro=False,
    )
    mon = next(ln for ln in bill["lines"] if ln["id"] == "monitoring")
    inv = next(ln for ln in bill["lines"] if ln["id"] == "invoicing")
    assert mon["billed"] is False
    assert inv["billed"] is True
    assert inv["amount_cents"] == 3000  # 2 × $15
    # Total = invoicing only
    assert bill["total_cents"] == 3000


def test_monitoring_only_still_shows_offtaker_line():
    """The bug Ford hit: monitoring/default plan hid offtaker pricing on Your Bill."""
    bill = build_unified_bill(
        billing_plan="monitoring",
        nameplate_kw=100,
        offtaker_count=4,
        ai_pro=False,
    )
    inv = next(ln for ln in bill["lines"] if ln["id"] == "invoicing")
    assert inv["quantity"] == 4
    assert inv["full_unit_cents"] == 1500
    assert inv["amount_cents"] == 6000
    assert inv["billed"] is False
    # Monitoring still charged; offtaker estimate not in total
    assert bill["total_cents"] == 1500  # 100 kW × $0.15
    assert any(ln["id"] == "collection_fee" for ln in bill["lines"])
