"""Offtaker Exchange — vacancy computation (v0, single-player-valuable).

Group net metering's structural waste is UNALLOCATED EXCESS: an array's host
account sends excess to the grid, some of it is shared out to group members
(offtakers) by GMP's percent method, and whatever nobody absorbs is retained on
the host account — cashed as a small credit or, more often, BANKED for up to ~12
months and then EVAPORATED. That retained-and-banked slice is the "vacancy": the
credit value a host is leaving on the table every month.

This module measures each array's vacancy two independent ways and reconciles
them with an honest confidence tier (per memory `ao-data-honesty-audit`):

  Estimator A — BILL-SIDE (primary, ground truth). The host bill IS the
    measurement. Under GMP's percent method, excess SHARED OUT to members shows
    on the host bill as an EXCESS line at $0 (its value went to the members);
    excess RETAINED by the host shows as a credited residual line (the exact line
    `rate_schedule.excess_credit_rate_from_bill` isolates) or simply banks
    (solar_credit_usd NULL). So per host bill:
        pool_kwh     = Bill.kwh_sent_to_grid                (the group pool)
        shared_kwh   = Σ EXCESS-line unitCount at $0        (allocated to members)
        retained_kwh = max(pool_kwh − shared_kwh, credited_kwh)   ← the vacancy
    Value = retained_kwh × the bill's own stated credit rate (→ account cash
    history → fleet reference → DEFAULT), never a fabricated flat rate.

  Estimator B — REGISTRY-SIDE (secondary, real-time). 1 − Σ(array_share_pct ??
    allocation_pct) over the array's active subscriptions — the server mirror of
    the frontend's `groupOfftakersByUtility`. Cheap and instant, but OVERSTATES
    vacancy whenever group members exist outside AO (GMP publishes no membership
    table — memory `offtaker-subaccount-master-child-picker`).

  Confidence — high when A and B agree within tolerance; medium when only A is
    available (operator doesn't bill members through AO) or A and B drift; none
    when there are no host bills (we say "connect the utility login to measure",
    never estimate vacancy from DailyGeneration × an invented export fraction).

⚠️ ASSUMPTION TO SPOT-CHECK ON REAL GMP JSON (honest gap): this reads $0 EXCESS
lines as SHARED-to-members and non-$0 retained lines / banked pool as VACANT — the
same interpretation `excess_credit_rate_from_bill` already encodes. A bank-only
host (Bruce's Londonderry) must therefore NOT print its banked excess as a $0
"Group Excess Shared" line, or bill-side would read it as fully allocated. The
registry estimator + confidence tier catch that disagreement, but before trusting
the bill-side number in prod, eyeball one real Londonderry host-bill raw_json.

NO cross-tenant leakage: everything here is called per tenant_id. `is_synthetic_
tenant()` guards any future cross-tenant aggregate (the demand board), excluding
is_demo AND the two known unflagged demo tenants.

NO money: this module computes and reads only. There is zero Stripe / fee / charge
here. The v1 placement fee lands elsewhere (see the plan's §6); this is the
instrumentation that makes the market visible first.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from .models import now as _now

logger = logging.getLogger(__name__)

# Trailing window of settled months to measure vacancy over.
VACANCY_WINDOW_MONTHS = 12
# A credit generated in month M expires ~12 months later (statewide guidance;
# GMP/VEC exact bookkeeping still to be verified before copy states hard dates —
# say "approaching expiry", not a date). Warn when within this many months.
CREDIT_LIFETIME_MONTHS = 12
EXPIRY_WARN_MONTHS = 2
# Bill-vs-registry agreement tolerance, as a FRACTION of the pool (5 points) —
# the spirit of reconcile_bills' allocation tolerance, expressed as a share.
CONFIDENCE_TOL_FRAC = 0.05

# Known demo/synthetic tenants that carry is_demo=False (2026-07-16 recon) — they
# must never seed the cross-tenant demand board with phantom supply.
SYNTHETIC_TENANT_IDS = {"ten_demo_realistic", "ten_ford_demo_100"}


def is_synthetic_tenant(t) -> bool:
    """True for demo/synthetic tenants that must be excluded from any CROSS-tenant
    aggregate (the future demand board). Own-tenant vacancy is fine to show a demo
    tenant — this guard is only for pooled/cross-tenant surfaces."""
    if t is None:
        return True
    if getattr(t, "is_demo", False):
        return True
    if getattr(t, "id", None) in SYNTHETIC_TENANT_IDS:
        return True
    if (getattr(t, "plan", None) or "").lower() == "demo":
        return True
    return False


# ── line-item walker: split a host bill's excess into shared vs retained ──────

def split_excess_line_items(raw_json: Optional[dict]) -> dict:
    """Walk a GMP bill's page-2 line items and split its EXCESS (kWh-sent-to-grid)
    into the portion SHARED OUT to group members ($0 credit lines — value went to
    them) versus the portion RETAINED and CREDITED to the host (negative-$ lines).

    Returns {"shared_kwh", "credited_kwh", "has_lines"}. `has_lines` is False when
    the bill carries no parseable KWH excess line items (older/sparse captures) —
    the caller then falls back to the pool total rather than trusting a 0 split.

    Reuses the same _EXCESS_CODES the invoice engine parses, so the shape is real.
    """
    from .rate_schedule import _EXCESS_CODES, _f

    out = {"shared_kwh": 0.0, "credited_kwh": 0.0, "has_lines": False}
    if not isinstance(raw_json, dict):
        return out
    for seg in raw_json.get("billSegments", []) or []:
        for li in seg.get("segmentLineItems", []) or []:
            if li.get("unitOfMeasure") != "KWH":
                continue
            uc = li.get("unitCode")
            if uc not in _EXCESS_CODES:
                continue
            cnt = _f(li.get("unitCount"))
            da = _f(li.get("dollarAmount"))
            if not cnt or cnt <= 0:
                continue
            out["has_lines"] = True
            if da is not None and da < 0:
                out["credited_kwh"] += cnt          # retained + cashed by host
            else:
                out["shared_kwh"] += cnt            # $0 → allocated to members
    out["shared_kwh"] = round(out["shared_kwh"], 1)
    out["credited_kwh"] = round(out["credited_kwh"], 1)
    return out


def _host_account_id(db, array_id: int) -> Optional[int]:
    """The net-meter group HOST account = the lowest UtilityAccount.id on the array
    (mirrors delivery._array_group_excess_for_sub_inner and the frontend's
    hostByArray). None when the array has no utility account."""
    from .models import UtilityAccount
    return db.execute(
        select(UtilityAccount.id).where(UtilityAccount.array_id == array_id)
        .order_by(UtilityAccount.id)
    ).scalars().first()


def _bill_credit_rate(db, bill, host_account_id: int) -> float:
    """The $/kWh to value retained excess at, honestly: the bill's own stated
    credited-line rate → the host account's cashing history → the fleet reference
    → DEFAULT_CREDIT_RATE. Never a fabricated flat default when a real one exists."""
    from .rate_schedule import (excess_credit_rate_from_bill, _account_credit_rate,
                                _fleet_credit_rate, array_age_bucket,
                                DEFAULT_CREDIT_RATE)
    from .models import UtilityAccount, Array

    r = excess_credit_rate_from_bill(bill.raw_json) if getattr(bill, "raw_json", None) else None
    if r is not None:
        return round(float(r), 6)
    # cashed months on this host account
    r = _account_credit_rate(db, host_account_id)
    if r is not None:
        return round(float(r), 6)
    # fleet median for provider + age bucket
    acct = db.get(UtilityAccount, host_account_id)
    arr = db.get(Array, acct.array_id) if acct and acct.array_id else None
    provider = (acct.provider if acct else None)
    ped = bill.period_end.date() if isinstance(bill.period_end, datetime) else bill.period_end
    age = array_age_bucket(arr.first_connect_date if arr else None, ped)
    if provider:
        r = _fleet_credit_rate(db, provider=provider, age_bucket=age)
        if r is not None:
            return round(float(r), 6)
    return round(DEFAULT_CREDIT_RATE, 6)


def _registry_allocated_frac(db, array_id: int) -> Optional[float]:
    """Σ(array_share_pct ?? allocation_pct) over the array's ENABLED offtaker
    subscriptions — the registry-side view of how much of the array is spoken for.
    None when no enabled subscription references this array (registry can't speak)."""
    from .models import BillingReportSubscription
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.array_id == array_id,
            BillingReportSubscription.enabled.is_(True),
        )
    ).scalars().all()
    if not subs:
        return None
    total = 0.0
    for s in subs:
        share = s.array_share_pct if s.array_share_pct is not None else s.allocation_pct
        try:
            total += float(share) if share is not None else 0.0
        except (TypeError, ValueError):
            continue
    return total


# ── per-array vacancy ─────────────────────────────────────────────────────────

def array_vacancy(db, array, *, window_months: int = VACANCY_WINDOW_MONTHS) -> Optional[dict]:
    """Trailing-window vacancy for ONE array. Returns a JSON-friendly dict, or None
    when the array has no host account at all (nothing to measure)."""
    from .models import Bill

    array_id = array.id
    host_id = _host_account_id(db, array_id)
    reg_alloc = _registry_allocated_frac(db, array_id)
    reg_vac = (max(0.0, 1.0 - reg_alloc) if reg_alloc is not None else None)

    base = {
        "array_id": array_id,
        "array_name": getattr(array, "name", None),
        "host_account_id": host_id,
        "provider": None,
        "vacancy_kwh": 0.0,
        "vacancy_usd": 0.0,
        "pool_kwh": 0.0,
        "vacancy_frac": None,
        "registry_allocated_frac": (round(reg_alloc, 4) if reg_alloc is not None else None),
        "registry_vacancy_frac": (round(reg_vac, 4) if reg_vac is not None else None),
        "months_of_history": 0,
        "confidence": "none",
        "confidence_note": "",
        "credit_rate": None,
        "expiring_soon_kwh": 0.0,
        "expiring_soon_usd": 0.0,
        "expiring_soon_months": None,
    }

    if host_id is None:
        base["confidence_note"] = ("No utility account on this array yet — connect "
                                   "the host GMP/VEC login to measure vacancy.")
        return base

    from .models import UtilityAccount
    host = db.get(UtilityAccount, host_id)
    base["provider"] = (host.provider if host else None)

    since = _now() - timedelta(days=int(window_months) * 31 + 5)
    bills = db.execute(
        select(Bill).where(
            Bill.account_id == host_id,
            Bill.period_end.isnot(None),
            Bill.period_end >= since,
            Bill.kwh_sent_to_grid.isnot(None),
            Bill.kwh_sent_to_grid > 0,
        ).order_by(Bill.period_end.desc())
    ).scalars().all()

    if not bills:
        # No settled host bills with excess in the window. Registry may still speak.
        if reg_vac is not None:
            base["confidence"] = "medium"
            base["vacancy_frac"] = round(reg_vac, 4)
            base["confidence_note"] = (
                "No host bill with excess captured yet — this figure is the "
                "registry estimate (1 − entered offtaker shares). Connect the host "
                "bill to measure it against the meter.")
        else:
            base["confidence_note"] = ("No host bill with excess captured yet — "
                                       "connect the utility login to measure vacancy.")
        return base

    pool_total = 0.0
    retained_total = 0.0
    value_total = 0.0
    last_rate = None
    expiring_kwh = 0.0
    expiring_usd = 0.0
    expiring_months = None
    now_d = _now()

    for b in bills[:window_months]:
        pool = float(b.kwh_sent_to_grid)
        split = split_excess_line_items(getattr(b, "raw_json", None))
        if split["has_lines"]:
            retained = max(pool - split["shared_kwh"], split["credited_kwh"])
        else:
            # No line-item split available: we can't see any member allocation on
            # this bill, so the whole pool reads as retained. Registry-side (below)
            # corrects the confidence when it disagrees.
            retained = pool
        retained = max(0.0, min(retained, pool))
        rate = _bill_credit_rate(db, b, host_id)
        last_rate = rate
        value = retained * rate
        pool_total += pool
        retained_total += retained
        value_total += value

        # Expiry: a BANKED month (no cash credit) rolls forward toward the ~12-month
        # cliff. The oldest banked retained kWh in the window is nearest expiry.
        # (v1 refines this with host consumption drawing the FIFO ladder down.)
        banked = (b.solar_credit_usd is None or float(b.solar_credit_usd or 0) <= 0)
        if banked and retained > 0:
            ped = b.period_end.date() if isinstance(b.period_end, datetime) else b.period_end
            age_months = (now_d.date() - ped).days / 30.44 if ped else 0
            months_left = CREDIT_LIFETIME_MONTHS - age_months
            if months_left <= EXPIRY_WARN_MONTHS:
                expiring_kwh += retained
                expiring_usd += value
                m = max(0.0, months_left)
                expiring_months = m if expiring_months is None else min(expiring_months, m)

    base["pool_kwh"] = round(pool_total, 1)
    base["vacancy_kwh"] = round(retained_total, 1)
    base["vacancy_usd"] = round(value_total, 2)
    base["months_of_history"] = len(bills[:window_months])
    base["credit_rate"] = (round(last_rate, 5) if last_rate is not None else None)
    base["expiring_soon_kwh"] = round(expiring_kwh, 1)
    base["expiring_soon_usd"] = round(expiring_usd, 2)
    base["expiring_soon_months"] = (round(expiring_months, 1) if expiring_months is not None else None)

    bill_vac = (retained_total / pool_total) if pool_total > 0 else None
    base["vacancy_frac"] = (round(bill_vac, 4) if bill_vac is not None else None)

    # Confidence: reconcile bill-side (A) vs registry-side (B).
    if bill_vac is None:
        base["confidence"] = "none"
        base["confidence_note"] = "No usable host-bill excess to measure."
    elif reg_vac is None:
        base["confidence"] = "medium"
        base["confidence_note"] = (
            "Measured from the host bill. No offtaker shares are entered in AO for "
            "this array yet, so we can't corroborate against your billing setup — "
            "add members to raise confidence.")
    elif abs(bill_vac - reg_vac) <= CONFIDENCE_TOL_FRAC:
        base["confidence"] = "high"
        base["confidence_note"] = ("The host bill and your entered offtaker shares "
                                   "agree on this vacancy.")
    else:
        base["confidence"] = "medium"
        base["confidence_note"] = (
            f"The host bill shows ~{bill_vac*100:.0f}% unallocated but your entered "
            f"shares imply ~{reg_vac*100:.0f}% — likely group members billed outside "
            f"AO. Complete membership setup to reconcile (see the Bill audit tab).")
    return base


def tenant_vacancy(db, tenant_id: str, *, window_months: int = VACANCY_WINDOW_MONTHS) -> dict:
    """Vacancy across ALL of one tenant's arrays (tenant-scoped; no cross-tenant
    read). Arrays are ordered most-vacant-dollars first — the money leak on top.
    """
    from .models import Array

    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant_id)
        .order_by(Array.id)
    ).scalars().all()

    rows = []
    for a in arrays:
        if getattr(a, "excluded", False):
            continue
        v = array_vacancy(db, a, window_months=window_months)
        if v is None:
            continue
        # Only surface arrays that either measured a host bill or have an offtaker
        # registry to speak to — skip bare telemetry-only arrays (nothing to say).
        if v["months_of_history"] == 0 and v["registry_vacancy_frac"] is None:
            continue
        rows.append(v)

    rows.sort(key=lambda r: (r.get("vacancy_usd") or 0.0), reverse=True)

    totals = {
        "vacancy_kwh": round(sum((r.get("vacancy_kwh") or 0.0) for r in rows), 1),
        "vacancy_usd": round(sum((r.get("vacancy_usd") or 0.0) for r in rows), 2),
        "expiring_soon_kwh": round(sum((r.get("expiring_soon_kwh") or 0.0) for r in rows), 1),
        "expiring_soon_usd": round(sum((r.get("expiring_soon_usd") or 0.0) for r in rows), 2),
        "array_count": len(rows),
    }
    return {
        "arrays": rows,
        "totals": totals,
        "window_months": int(window_months),
        "generated_at": _now().isoformat(),
    }
