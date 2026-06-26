"""Invoice ↔ GMP-bill reconciliation (Array Operator).

Compares what our generated invoice uses for an offtaker against the utility's
OWN captured GMP bill for the same array + period — so an operator can trust the
numbers before sending. This is a READ-ONLY verification surface: it never
mutates bills or invoices and never fabricates. When no GMP bill is linked to an
array yet, the row is honestly reported as "awaiting GMP data" (status=no_bill).

Comparison, per offtaker subscription:
  • our invoice  → delivery.build_match(sub): the array kWh + period + the
                   offtaker's billed kWh (allocation_pct × array kWh).
  • the GMP bill → the Bill row for that array's utility account whose period
                   overlaps the invoice period (Bill.kwh_generated etc.).
  • verdict      → match | mismatch | no_bill | no_invoice_data, with the kWh
                   delta + % when both sides have data.

The unit we compare is PRODUCED kWh for the array over the period: our invoice's
array_kwh vs the GMP bill's kwh_generated. (Per-array, before the offtaker's %.)
That isolates "is our production number right vs. the meter" from allocation.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from sqlalchemy import func

from ..models import (
    BillingReportSubscription, Array, UtilityAccount, Bill, DailyGeneration,
)

# kWh agreement tolerance: GMP bills are whole-kWh; our daily sums can carry a
# rounding tail. Within this band (or this %) we call it a match.
_ABS_TOL_KWH = 1.0
_PCT_TOL = 0.01   # 1%


def _bill_for_array_period(
    db: Session, array_id: int, start: Optional[date], end: Optional[date]
) -> Optional[Bill]:
    """The captured GMP bill for this array whose period best matches [start,end].

    Prefers a bill whose period_end falls in the invoice window; else the latest
    bill for the array. Only bills with a real kwh_generated are considered.
    Returns None when the array has no linked GMP account / no bills.
    """
    q = (
        select(Bill)
        .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
        .where(UtilityAccount.array_id == array_id,
               Bill.kwh_generated.isnot(None))
        .order_by(Bill.period_end.desc())
    )
    bills = list(db.execute(q).scalars().all())
    if not bills:
        return None
    if end is not None:
        # Best overlap: a bill whose period_end is within ±20 days of our end.
        for b in bills:
            if b.period_end is None:
                continue
            be = b.period_end
            be = be.date() if hasattr(be, "date") else be
            try:
                if abs((be - end).days) <= 20:
                    return b
            except (TypeError, AttributeError):
                continue
    return bills[0]


def _verdict(our_kwh: Optional[float], gmp_kwh: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    """(status, delta_kwh, delta_pct). status ∈ match|mismatch|no_invoice_data|no_bill."""
    if gmp_kwh is None:
        return "no_bill", None, None
    if our_kwh is None:
        return "no_invoice_data", None, None
    delta = round(our_kwh - gmp_kwh, 2)
    base = gmp_kwh if gmp_kwh else 1.0
    pct = round(delta / base, 4)
    if abs(delta) <= _ABS_TOL_KWH or abs(pct) <= _PCT_TOL:
        return "match", delta, pct
    return "mismatch", delta, pct


def _mismatch_reason(
    db: Session,
    array_id: int,
    status: str,
    inv_start: Optional[date],
    inv_end: Optional[date],
    bill: Optional[Bill],
) -> Optional[str]:
    """Plain-English explanation of WHY a row doesn't cleanly match, grounded in
    real signals — so an operator can tell a data-quality artifact from a genuine
    billing-model problem before trusting the number. Never fabricates: returns
    None when the row is a clean match (no explanation needed).

      • no_bill        → no captured GMP bill linked to this array yet.
      • no_invoice_data→ our invoice produced no array kWh for the period.
      • mismatch       → distinguish (a) ESTIMATE: our production is a prorated
                         bill smear (no real meter reads in the window) from
                         (b) TIMING: the bill period and invoice period don't
                         overlap, from (c) a real per-array delta.
    """
    if status == "match":
        return None
    if status == "no_bill":
        return "No captured GMP bill linked to this array yet — awaiting utility data."
    if status == "no_invoice_data":
        return "Our invoice produced no kWh for this array over the period."

    # status == "mismatch": probe the real data source for the invoice window.
    metered = 0
    prorated = 0
    if inv_start is not None and inv_end is not None:
        metered = db.execute(
            select(func.count(DailyGeneration.id)).where(
                DailyGeneration.array_id == array_id,
                DailyGeneration.day >= inv_start,
                DailyGeneration.day <= inv_end,
                func.coalesce(DailyGeneration.source, "") != "bill_prorate",
            )
        ).scalar() or 0
        prorated = db.execute(
            select(func.count(DailyGeneration.id)).where(
                DailyGeneration.array_id == array_id,
                DailyGeneration.day >= inv_start,
                DailyGeneration.day <= inv_end,
                DailyGeneration.source == "bill_prorate",
            )
        ).scalar() or 0

    # Timing: does the bill's period actually overlap the invoice period?
    if bill is not None and inv_start is not None and inv_end is not None:
        bs = bill.period_start.date() if bill.period_start else None
        be = bill.period_end.date() if bill.period_end else None
        if bs is not None and be is not None and (be < inv_start or bs > inv_end):
            return ("Bill period and invoice period don't overlap — comparing "
                    "different months, so the delta is a timing gap, not a real "
                    "discrepancy.")

    if metered == 0 and prorated > 0:
        return ("Our figure is a prorated bill estimate (no metered daily reads "
                "in this window yet) — expect drift vs. the bill total until real "
                "reads land.")
    if metered == 0 and prorated == 0:
        return ("No daily generation rows for this array in the period — our kWh "
                "comes from the invoice fallback, not measured reads.")
    if prorated > 0:
        return ("Period mixes measured reads with prorated bill estimates — "
                "partial data, so some drift is expected.")
    return ("Both sides have real data — this delta reflects a genuine "
            "production/billing difference worth a closer look.")


def reconcile_subscription(db: Session, sub: BillingReportSubscription) -> dict:
    """Compare one offtaker's invoice production figures against GMP bills.

    Returns {customer_name, sub_id, arrays:[{array_id, array_name, our_kwh,
    gmp_kwh, status, delta_kwh, delta_pct, period}], overall_status}.
    Per-array so a multi-array offtaker shows one row per array.
    """
    # Lazy import to avoid a cycle (delivery imports models/this package).
    from .delivery import build_match, _normalized_allocations

    try:
        match = build_match(sub)
    except Exception:  # never let one bad sub break the report
        match = None

    # Map array_id -> our produced kWh for the period, from the match breakdown.
    our_by_array: dict[int, float] = {}
    inv_period = (None, None)
    if match is not None:
        ci = match.computed_invoice or {}
        bd = ci.get("array_breakdown") or (match.project_totals or {}).get("array_breakdown")
        if bd:
            for b in bd:
                if b.get("array_id") is not None:
                    our_by_array[int(b["array_id"])] = float(b.get("array_kwh") or 0)
        elif getattr(sub, "array_id", None) is not None:
            our_by_array[int(sub.array_id)] = float(ci.get("array_kwh") or 0)
        ps, pe = ci.get("period_start"), ci.get("period_end")
        inv_period = (ps, pe)

    # The set of arrays this sub bills against.
    allocs = _normalized_allocations(sub)
    aids = [a["array_id"] for a in allocs] if allocs else (
        [sub.array_id] if getattr(sub, "array_id", None) is not None else [])

    def _as_date(v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                return None
        return v.date() if hasattr(v, "date") else v

    start = _as_date(inv_period[0])
    end = _as_date(inv_period[1])

    rows = []
    for aid in aids:
        arr = db.get(Array, aid)
        our_kwh = our_by_array.get(aid)
        bill = _bill_for_array_period(db, aid, start, end)
        gmp_kwh = float(bill.kwh_generated) if (bill and bill.kwh_generated is not None) else None
        status, delta, pct = _verdict(our_kwh, gmp_kwh)
        bp = None
        if bill is not None:
            bs = bill.period_start.date().isoformat() if bill.period_start else None
            be = bill.period_end.date().isoformat() if bill.period_end else None
            bp = f"{bs} → {be}"
        rows.append({
            "array_id": aid,
            "array_name": arr.name if arr else f"Array {aid}",
            "our_kwh": round(our_kwh, 1) if our_kwh is not None else None,
            "gmp_kwh": round(gmp_kwh, 1) if gmp_kwh is not None else None,
            "delta_kwh": delta,
            "delta_pct": pct,
            "status": status,
            "mismatch_reason": _mismatch_reason(db, aid, status, start, end, bill),
            "gmp_bill_period": bp,
            "gmp_total_cost": (float(bill.total_cost) if (bill and bill.total_cost is not None) else None),
        })

    # Roll up: mismatch dominates, then no_bill, then no_invoice_data, else match.
    statuses = {r["status"] for r in rows}
    if not rows:
        overall = "no_arrays"
    elif "mismatch" in statuses:
        overall = "mismatch"
    elif statuses == {"match"}:
        overall = "match"
    elif "no_bill" in statuses and "match" not in statuses:
        overall = "no_bill"
    else:
        overall = "partial"

    return {
        "sub_id": sub.id,
        "customer_name": sub.customer_name,
        "from_workbook": bool(getattr(sub, "source_workbook", None)),
        "invoice_period": (f"{start.isoformat()} → {end.isoformat()}"
                           if (start and end) else None),
        "arrays": rows,
        "overall_status": overall,
    }


def reconcile_tenant(db: Session, tenant_id: str) -> dict:
    """Reconcile every active subscription for a tenant. Summary + per-sub rows."""
    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
        .order_by(BillingReportSubscription.id)
    ).scalars().all()

    results = [reconcile_subscription(db, s) for s in subs]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["overall_status"]] = counts.get(r["overall_status"], 0) + 1
    return {
        "ok": True,
        "subscription_count": len(results),
        "status_counts": counts,
        "subscriptions": results,
    }
