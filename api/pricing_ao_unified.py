"""Unified Array Operator commercial model (display + AI freemium).

Three product charge lines (existing Stripe meters) + one AI add-on:

  1. Fleet monitoring  — registered nameplate kW × graduated $/kW
                         (api/pricing_ao_nameplate.py)
  2. Offtaker invoices — offtaker count × graduated $/offtaker
                         (api/pricing_ao_invoicing.py)
  3. Energy Agent Pro  — flat $50/mo for unlimited AI (this module)

Free-tier AI: every tenant gets a small weekly sample budget so they can try
the integrated agent without paying. Default $2.50/week. Pro lifts the cap.

This module is the SINGLE place for the AI freemium numbers and the unified
bill breakdown shape the Account tab renders. Stripe price minting for AI Pro
is env-driven (STRIPE_AO_AI_PRO_PRICE_ID) — do not invent live prices here.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from .pricing_ao_invoicing import (
    PER_OFFTAKER_CENTS as OFFTAKER_FULL_CENTS,
    blended_unit_cents as offtaker_blended_cents,
    compute_monthly_cents as offtaker_monthly_cents,
)
from .pricing_ao_nameplate import (
    FULL_UNIT_CENTS as NAMEPLATE_FULL_CENTS,
    compute_monthly_cents as nameplate_monthly_cents,
)
from .pricing_ao_genreports import PRICE_CENTS as GENREPORT_ARRAY_CENTS


def _collection_fee_info() -> dict:
    """Online offtaker pay-link platform skim (not a monthly SaaS line)."""
    try:
        from .billing.payments import fee_bps
        bps = int(fee_bps())
    except Exception:
        bps = 50
    bps = max(0, bps)
    pct = bps / 100.0  # 50 bps → 0.5
    return {
        "id": "collection_fee",
        "kind": "Online offtaker payments",
        "basis": "percent_of_collected",
        "fee_bps": bps,
        "fee_percent": pct,
        "amount_cents": None,  # not a fixed monthly charge
        "included_in_monthly_total": False,
        "desc": (
            f"When an offtaker pays an invoice online (card/bank via Stripe), "
            f"we keep {pct:g}% of that payment as a platform fee. "
            f"The rest goes to you. Check and offline payments: no fee. "
            f"This is not part of your monthly bill above."
        ),
    }

# ── Energy Agent freemium ────────────────────────────────────────────────────
# Free weekly sample (thinking + voice). Pro = unlimited for a flat monthly fee.
AI_FREE_WEEKLY_BUDGET_USD: float = float(
    os.getenv("ENERGY_AGENT_WEEKLY_BUDGET_USD", "2.5")
)
AI_PRO_MONTHLY_USD: float = float(os.getenv("ENERGY_AGENT_PRO_MONTHLY_USD", "50"))
AI_PRO_MONTHLY_CENTS: int = int(round(AI_PRO_MONTHLY_USD * 100))
# Stripe price id once minted (optional until Ford confirms money side).
AI_PRO_PRICE_ID: str = (os.getenv("STRIPE_AO_AI_PRO_PRICE_ID") or "").strip()

# Product plan labels for the Account UI
PLAN_LABELS = {
    "monitoring": "Live vendor data",
    "invoicing": "Offtaker invoices",
    "both": "Both — monitoring + invoices",
}


def tenant_has_ai_pro(tenant: Any) -> bool:
    """Unlimited Energy Agent when Pro is on, or the account is comped/demo."""
    if getattr(tenant, "ai_pro", False):
        return True
    plan = (getattr(tenant, "plan", None) or "").strip().lower()
    if plan in ("comped", "demo"):
        return True
    if bool(getattr(tenant, "is_demo", False)):
        return True
    # Explicit unlimited env override for internal tenants
    if os.getenv("ENERGY_AGENT_UNLIMITED", "").strip() in ("1", "true", "yes"):
        return True
    return False


def ai_budget_cap_usd(tenant: Any) -> Optional[float]:
    """Weekly $ cap, or None when unlimited (Pro)."""
    if tenant_has_ai_pro(tenant):
        return None
    return max(0.01, float(AI_FREE_WEEKLY_BUDGET_USD))


def build_unified_bill(
    *,
    billing_plan: Optional[str] = None,
    nameplate_kw: float = 0.0,
    offtaker_count: int = 0,
    ai_pro: bool = False,
    genreport_array_quarters: int = 0,
    include_monitoring: Optional[bool] = None,
    include_invoicing: Optional[bool] = None,
    always_show_product_lines: bool = True,
) -> dict:
    """Itemized monthly product bill + AI Pro for the Account Billing section.

    Regular AO product (Jul 2026): ALWAYS fleet monitoring (nameplate kW) +
    offtaker invoices (count). billing_plan / include_* are ignored for what is
    *charged* (kept for call-site compatibility). AI Pro is the only add-on.

    Amounts in cents (may be fractional for nameplate half-cents).
    """
    # Regular product always bills both meters (plan split retired).
    mon_billed = True if include_monitoring is None else bool(include_monitoring)
    inv_billed = True if include_invoicing is None else bool(include_invoicing)
    if always_show_product_lines:
        mon_billed = True
        inv_billed = True

    lines: list[dict] = []
    total = 0.0

    mon = float(nameplate_monthly_cents(nameplate_kw) or 0.0)
    full_m = float(NAMEPLATE_FULL_CENTS)
    blended_m = (mon / float(nameplate_kw)) if nameplate_kw and nameplate_kw > 0 else full_m
    if mon_billed:
        total += mon
    lines.append({
        "id": "monitoring",
        "kind": "Fleet monitoring",
        "basis": "nameplate_kw",
        "quantity": round(float(nameplate_kw or 0), 2),
        "unit_label": "kW",
        "unit_cents": round(blended_m, 3),
        "full_unit_cents": full_m,
        "amount_cents": round(mon, 2),
        "billed": mon_billed,
        "included_in_monthly_total": mon_billed,
        "desc": (
            "Live fleet health — billed on registered inverter nameplate (kW). "
            "Volume discounts apply automatically for large fleets."
        ),
    })

    n = int(offtaker_count or 0)
    inv = float(offtaker_monthly_cents(n) or 0.0)
    full_i = float(OFFTAKER_FULL_CENTS)
    blended_i = float(offtaker_blended_cents(n) or full_i)
    if inv_billed:
        total += inv
    lines.append({
        "id": "invoicing",
        "kind": "Offtaker invoices",
        "basis": "offtaker_count",
        "quantity": n,
        "unit_label": "offtakers",
        "unit_cents": round(blended_i, 2),
        "full_unit_cents": full_i,
        "amount_cents": round(inv, 2),
        "billed": inv_billed,
        "included_in_monthly_total": inv_billed,
        "desc": (
            f"${full_i / 100:.0f} per offtaker / month for every active offtaker "
            "on your roster (volume discounts as you grow)."
        ),
    })

    # ── Generation reports (THE FOLD, Jul 2026) — METERED, $15 per ARRAY per
    # quarter, charged on the first real output (send or download) covering that
    # array, then unlimited that quarter. Unlike the two lines above (recurring
    # subscriptions), this is USAGE already accrued this billing period, so the
    # quantity is a COUNT OF REPORTED ARRAYS, not a roster size. Building,
    # previewing and auto-propagating the fleet is free — a $0 line here means the
    # operator hasn't reported anything this period, not that the feature is off.
    gr = int(genreport_array_quarters or 0)
    gr_total = float(gr * GENREPORT_ARRAY_CENTS)
    if gr:
        total += gr_total
    lines.append({
        "id": "generation_reports",
        "kind": "Generation reports",
        "basis": "array_quarters",
        "quantity": gr,
        "unit_label": "arrays reported",
        "unit_cents": float(GENREPORT_ARRAY_CENTS),
        "full_unit_cents": float(GENREPORT_ARRAY_CENTS),
        "amount_cents": round(gr_total, 2),
        "billed": bool(gr),
        "included_in_monthly_total": bool(gr),
        "metered": True,
        "desc": (
            f"${GENREPORT_ARRAY_CENTS / 100:.0f} per array, once per quarter — charged "
            "the first time you report an array (send its report or download the "
            "workbook), then unlimited that quarter. Building and previewing are free."
        ),
    })

    ai_line = {
        "id": "ai_pro",
        "kind": "Energy Agent Pro",
        "basis": "flat",
        "quantity": 1 if ai_pro else 0,
        "unit_label": "month",
        "unit_cents": float(AI_PRO_MONTHLY_CENTS),
        "full_unit_cents": float(AI_PRO_MONTHLY_CENTS),
        "amount_cents": float(AI_PRO_MONTHLY_CENTS) if ai_pro else 0.0,
        "included": bool(ai_pro),
        "billed": bool(ai_pro),
        "included_in_monthly_total": bool(ai_pro),
        "desc": (
            f"Unlimited integrated AI (thinking + voice). "
            f"Without Pro you get a ${AI_FREE_WEEKLY_BUDGET_USD:.2f}/week sample."
        ),
    }
    if ai_pro:
        total += float(AI_PRO_MONTHLY_CENTS)
    lines.append(ai_line)

    # Transparency only — never added to total_cents.
    collection = _collection_fee_info()
    lines.append({
        "id": collection["id"],
        "kind": collection["kind"],
        "basis": collection["basis"],
        "quantity": 0,
        "unit_label": f"{collection['fee_percent']:g}% of online payments",
        "unit_cents": 0,
        "full_unit_cents": 0,
        "amount_cents": None,
        "billed": False,
        "included_in_monthly_total": False,
        "fee_bps": collection["fee_bps"],
        "fee_percent": collection["fee_percent"],
        "desc": collection["desc"],
    })

    return {
        "plan": "regular",
        "plan_label": "Regular",
        "lines": lines,
        "total_cents": round(total, 2),
        "ai": {
            "pro": bool(ai_pro),
            "monthly_usd": AI_PRO_MONTHLY_USD,
            "free_weekly_usd": AI_FREE_WEEKLY_BUDGET_USD,
            "stripe_price_ready": bool(AI_PRO_PRICE_ID),
        },
        "collection_fee": collection,
        "model_note": (
            "Regular plan: fleet monitoring (kW nameplate) + offtaker invoices "
            f"(${OFFTAKER_FULL_CENTS / 100:.0f}/offtaker). Generation reports are "
            f"${GENREPORT_ARRAY_CENTS / 100:.0f} per array, once per quarter — only "
            "for arrays you actually report; building and previewing are free. "
            f"Energy Agent Pro is ${AI_PRO_MONTHLY_USD:.0f}/mo unlimited AI "
            f"(free tier keeps a ${AI_FREE_WEEKLY_BUDGET_USD:.2f}/week sample). "
            f"When offtakers pay online we keep {collection['fee_percent']:g}% "
            "of that payment — not part of the monthly total."
        ),
    }
