"""Offtaker Exchange — match suggestions + lead→offtaker draft helpers.

v1 facilitation (still human-approved):
  • Parse a free-text desired kWh band into a monthly number when possible.
  • Suggest (lead, vacant array) pairings inside the SAME utility territory.
  • Never auto-enrolls; never moves money; never changes utility membership.

AO remains software + paper. Host confirms every placement.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Statuses the operator desk understands (stored as free strings on ExchangeDemand).
LEAD_STATUSES = frozenset({
    "new", "suggested", "drafted", "utility_pending", "live", "dead", "qualified", "matched",
})


def parse_desired_kwh_mo(band: Optional[str]) -> Optional[float]:
    """Best-effort parse of waitlist free text → kWh/month.

    Accepts shapes like '~2,000 kWh/mo', '1500', '2.5 MWh/mo'. Returns None when
    nothing numeric is recoverable — suggestions still work on utility alone.
    """
    if not band:
        return None
    s = str(band).strip().lower().replace(",", "")
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(mwh|mw\.?h)?", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").replace(".", "")
    if unit.startswith("mwh") or unit == "mwh":
        n *= 1000.0  # MWh/mo → kWh/mo
    # Guard nonsense
    if n <= 0 or n > 500_000:
        return None
    return round(n, 1)


def _norm_util(u: Optional[str]) -> str:
    return (u or "").strip().lower()


def suggest_pairings(
    *,
    vacancies: list[dict[str, Any]],
    leads: list[Any],
    max_per_lead: int = 3,
) -> list[dict[str, Any]]:
    """Rank (lead × vacant array) pairs. Tenant-scoped inputs only.

    Score (higher better):
      + utility match (hard gate if both sides have a utility)
      + vacancy_frac / vacancy_usd urgency
      + size fit when desired_kwh_mo is known vs monthly vacancy
      + expiring_soon boost
    """
    out: list[dict[str, Any]] = []
    # Only arrays with something to place
    vacs = [
        v for v in vacancies
        if (v.get("vacancy_frac") or 0) > 0.02
        or (v.get("vacancy_kwh") or 0) > 0
        or (v.get("expiring_soon_kwh") or 0) > 0
    ]
    if not vacs or not leads:
        return out

    for lead in leads:
        status = (getattr(lead, "status", None) or "new").lower()
        if status in ("dead", "live", "drafted", "utility_pending"):
            continue
        lead_util = _norm_util(getattr(lead, "utility", None))
        desired = parse_desired_kwh_mo(getattr(lead, "desired_band", None))
        scored: list[tuple[float, dict]] = []
        for v in vacs:
            v_util = _norm_util(v.get("provider"))
            if lead_util and v_util and lead_util != v_util:
                continue
            if lead_util and not v_util:
                continue
            score = 0.0
            reasons: list[str] = []
            if lead_util and v_util and lead_util == v_util:
                score += 50
                reasons.append(f"same utility ({v_util.upper()})")
            elif not lead_util:
                score += 10
                reasons.append("lead has no utility — confirm territory")

            frac = float(v.get("vacancy_frac") or 0)
            score += min(25.0, frac * 40.0)
            usd = float(v.get("vacancy_usd") or 0)
            if usd > 0:
                score += min(15.0, usd / 500.0)
            exp_k = float(v.get("expiring_soon_kwh") or 0)
            if exp_k > 0:
                score += 20
                reasons.append("credits approaching expiry")

            pool = float(v.get("pool_kwh") or 0)
            vac_kwh = float(v.get("vacancy_kwh") or 0)
            # Monthly vacancy ≈ trailing-12 / 12
            vac_mo = vac_kwh / 12.0 if vac_kwh else 0.0
            suggested_alloc = None
            if desired and pool > 0:
                # annual desire / annual pool
                annual_desire = desired * 12.0
                suggested_alloc = round(min(0.95, max(0.01, annual_desire / pool)), 4)
                # Fit: how close desire is to monthly vacancy
                if vac_mo > 0:
                    ratio = desired / vac_mo
                    if 0.4 <= ratio <= 1.25:
                        score += 20
                        reasons.append("size fits vacancy")
                    elif ratio < 0.4:
                        score += 8
                        reasons.append("smaller than vacancy (easy fit)")
                    else:
                        score += 2
                        reasons.append("wants more than measured vacancy — split or waitlist")
                reasons.append(f"suggested share ≈ {suggested_alloc*100:.1f}% of pool")
            elif frac > 0:
                # Default suggestion: place half the measured vacancy, capped
                suggested_alloc = round(min(0.5, max(0.05, frac * 0.5)), 4)
                reasons.append(f"suggested share ≈ {suggested_alloc*100:.1f}% (half of measured vacancy)")

            if score <= 0:
                continue
            scored.append((score, {
                "lead_id": lead.id,
                "lead_name": getattr(lead, "contact_name", None),
                "lead_email": getattr(lead, "contact_email", None),
                "lead_utility": getattr(lead, "utility", None),
                "lead_desired_band": getattr(lead, "desired_band", None),
                "desired_kwh_mo": desired,
                "array_id": v.get("array_id"),
                "array_name": v.get("array_name"),
                "provider": v.get("provider"),
                "vacancy_frac": v.get("vacancy_frac"),
                "vacancy_kwh": v.get("vacancy_kwh"),
                "vacancy_usd": v.get("vacancy_usd"),
                "expiring_soon_kwh": v.get("expiring_soon_kwh"),
                "expiring_soon_usd": v.get("expiring_soon_usd"),
                "confidence": v.get("confidence"),
                "suggested_allocation_pct": suggested_alloc,
                "score": round(score, 2),
                "reasons": reasons,
            }))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, row in scored[:max_per_lead]:
            out.append(row)

    out.sort(key=lambda r: r.get("score") or 0, reverse=True)
    return out


def lead_dict(r) -> dict[str, Any]:
    return {
        "id": r.id,
        "contact_name": r.contact_name,
        "contact_email": r.contact_email,
        "contact_phone": r.contact_phone,
        "utility": r.utility,
        "desired_band": r.desired_band,
        "desired_kwh_mo": parse_desired_kwh_mo(r.desired_band),
        "monthly_bill_usd": r.monthly_bill_usd,
        "source": r.source,
        "status": r.status,
        "notes": r.notes,
        "suggested_array_id": getattr(r, "suggested_array_id", None),
        "linked_subscription_id": getattr(r, "linked_subscription_id", None),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
