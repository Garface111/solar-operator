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
    billing_plan: Optional[str],
    nameplate_kw: float = 0.0,
    offtaker_count: int = 0,
    ai_pro: bool = False,
    include_monitoring: Optional[bool] = None,
    include_invoicing: Optional[bool] = None,
) -> dict:
    """Itemized monthly product bill + AI line for the Account Billing section.

    include_* default from billing_plan (monitoring | invoicing | both).
    Amounts in cents (may be fractional for nameplate half-cents).
    """
    p = (billing_plan or "").strip().lower()
    if include_monitoring is None:
        include_monitoring = p in ("monitoring", "both", "")
    if include_invoicing is None:
        include_invoicing = p in ("invoicing", "both")

    lines: list[dict] = []
    total = 0.0

    if include_monitoring:
        mon = float(nameplate_monthly_cents(nameplate_kw) or 0.0)
        full = float(NAMEPLATE_FULL_CENTS)
        blended = (mon / float(nameplate_kw)) if nameplate_kw and nameplate_kw > 0 else full
        total += mon
        lines.append({
            "id": "monitoring",
            "kind": "Fleet monitoring",
            "basis": "nameplate_kw",
            "quantity": round(float(nameplate_kw or 0), 2),
            "unit_label": "kW",
            "unit_cents": round(blended, 3),
            "full_unit_cents": full,
            "amount_cents": round(mon, 2),
            "desc": (
                "Live fleet health, billed on registered inverter nameplate. "
                "Volume discounts apply automatically for large fleets."
            ),
        })

    if include_invoicing:
        n = int(offtaker_count or 0)
        inv = float(offtaker_monthly_cents(n) or 0.0)
        full = float(OFFTAKER_FULL_CENTS)
        blended = float(offtaker_blended_cents(n) or full)
        total += inv
        lines.append({
            "id": "invoicing",
            "kind": "Offtaker invoices",
            "basis": "offtaker_count",
            "quantity": n,
            "unit_label": "offtakers",
            "unit_cents": round(blended, 2),
            "full_unit_cents": full,
            "amount_cents": round(inv, 2),
            "desc": (
                "Automatic offtaker solar-credit invoices. "
                "Volume discounts apply as your roster grows."
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
        "desc": (
            f"Unlimited integrated AI (thinking + voice). "
            f"Without Pro you get a ${AI_FREE_WEEKLY_BUDGET_USD:.2f}/week sample."
        ),
    }
    if ai_pro:
        total += float(AI_PRO_MONTHLY_CENTS)
    lines.append(ai_line)

    return {
        "plan": p or "both",
        "plan_label": PLAN_LABELS.get(p or "both", "Array Operator"),
        "lines": lines,
        "total_cents": round(total, 2),
        "ai": {
            "pro": bool(ai_pro),
            "monthly_usd": AI_PRO_MONTHLY_USD,
            "free_weekly_usd": AI_FREE_WEEKLY_BUDGET_USD,
            "stripe_price_ready": bool(AI_PRO_PRICE_ID),
        },
        "model_note": (
            "Three independent lines: monitoring (kW), offtaker invoices (count), "
            "and optional Energy Agent Pro ($50/mo unlimited AI). "
            "Free accounts keep a small weekly AI sample so you can try the agent."
        ),
    }
