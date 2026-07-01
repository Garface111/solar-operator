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

# Allocation cross-check tolerance (Anna/Bruce's "catch GMP errors" ask). GMP
# allocates an array's group-excess to each offtaker by share%, to whole kWh; a
# clean allocation lands within a hair of share × array-total. Beyond this band
# the offtaker's credited kWh implies a group total that doesn't match the array
# bill — the exact discrepancy Anna is paid $25 to catch. Kept tight (2 kWh, a
# hair above whole-kWh rounding) because the whole point is small allocation
# errors; we always surface the full math so the operator makes the final call.
_ALLOC_TOL_KWH = 2.0


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


def _array_group_excess(bill: Optional[Bill]) -> Optional[float]:
    """The array's group-excess pool for the period = the EXCESS sent to grid on
    the array's own GMP bill (kwh_sent_to_grid). This is the number GMP allocates
    among the array's offtakers by share%. Falls back to net generated only when
    an older parse never captured sent-to-grid."""
    if bill is None:
        return None
    v = getattr(bill, "kwh_sent_to_grid", None)
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return float(bill.kwh_generated) if bill.kwh_generated is not None else None


def _allocation_check(
    db: Session, sub, aids: list[int], share_by_array: dict[int, float],
    array_bill_by_aid: dict[int, Optional[Bill]], target_label: Optional[str],
) -> dict:
    """Anna/Bruce's allocation cross-check: does the excess GMP actually credited
    THIS offtaker (from their own bill) match their share of the array's group
    excess (from the array's bill)? Reverse-solves the group total GMP implied
    (credited ÷ share) and compares it to the array bill's stated group excess —
    surfacing an allocation error GMP buried in a base that appears on neither
    bill (Bruce: 7,343 = 25.53% of 28,762, not the bill's 28,772).

    Never fabricates a clean 'match': when the check can't run honestly (no
    offtaker account, no share, single-meter setup, missing bills) it returns that
    exact state with a plain-English reason so the operator knows why.
    """
    oa_id = getattr(sub, "utility_account_id", None)
    if oa_id is None:
        return {"status": "no_offtaker_account",
                "note": "This offtaker isn't bound to their own utility account, so "
                        "there's no GMP-credited figure to cross-check."}

    # The offtaker's OWN credited excess — the exact number the invoice bills on,
    # via the same resolver, so the check verifies what we actually charge.
    from ..rate_schedule import resolve_offtaker_excess_credit
    try:
        r = resolve_offtaker_excess_credit(db, oa_id, target_label)
    except Exception:
        r = None
    offtaker_excess = float(r[0]) if (r and r[0] is not None) else None
    credit_rate = float(r[2]) if (r and len(r) > 2 and r[2] is not None) else None
    if offtaker_excess is None:
        return {"status": "no_offtaker_bill",
                "note": "No GMP bill with billable excess for this offtaker yet — "
                        "the allocation check runs once their bill lands."}

    # Expected = Σ (array group-excess × this offtaker's share of that array).
    # Single-array offtakers (Bruce's case) → one term; the sum generalizes to
    # multi-array without changing the math.
    expected = 0.0
    array_excess_total = 0.0
    have_array_excess = False
    single_meter = False
    per_array = []
    for aid in aids:
        bill = array_bill_by_aid.get(aid)
        if bill is not None and getattr(bill, "account_id", None) == oa_id:
            single_meter = True
        ax = _array_group_excess(bill)
        share = share_by_array.get(aid)
        if ax is not None and share:
            have_array_excess = True
            expected += ax * share
            array_excess_total += ax
            per_array.append({"array_id": aid, "array_excess_kwh": round(ax, 1),
                              "share": round(share, 6), "expected_kwh": round(ax * share, 1)})

    if single_meter:
        return {"status": "single_meter",
                "note": "This offtaker is billed on the array's own meter (no separate "
                        "GMP sub-account), so there's no allocation to cross-check — the "
                        "array-level check above is the audit."}
    if not any(share_by_array.get(a) for a in aids):
        return {"status": "no_share",
                "note": "No share % entered for this offtaker — enter their share of the "
                        "array to cross-check GMP's allocation."}
    if not have_array_excess:
        return {"status": "no_array_bill",
                "note": "No array GMP bill with group-excess captured for this period yet "
                        "— the allocation check runs once it lands."}

    delta = round(offtaker_excess - expected, 2)
    # Reverse-solved group total GMP implied for this offtaker (single-array only:
    # credited ÷ share) — Bruce's headline "number that appears nowhere".
    single_share = share_by_array.get(aids[0]) if len(aids) == 1 else None
    implied_base = round(offtaker_excess / single_share, 1) if single_share else None
    dollars = round(abs(delta) * credit_rate, 2) if credit_rate else None
    status = "match" if abs(delta) <= _ALLOC_TOL_KWH else "mismatch"

    out = {
        "status": status,
        "offtaker_credited_kwh": round(offtaker_excess, 1),
        "expected_kwh": round(expected, 1),
        "delta_kwh": delta,
        "implied_group_total_kwh": implied_base,
        "array_group_excess_kwh": round(array_excess_total, 1),
        "credit_rate": round(credit_rate, 5) if credit_rate else None,
        "delta_dollars": dollars,
        "per_array": per_array,
    }
    if status == "mismatch":
        if implied_base is not None and array_excess_total:
            head = (f"GMP credited this offtaker {offtaker_excess:,.0f} kWh — at the "
                    f"{single_share*100:.2f}% share you entered that implies a group total "
                    f"of {implied_base:,.0f} kWh, but the array's bill shows "
                    f"{array_excess_total:,.0f} kWh")
        else:
            head = (f"GMP credited this offtaker {offtaker_excess:,.0f} kWh, but their share "
                    f"of the array's group excess is {expected:,.0f} kWh")
        tail = f" (off by {abs(delta):,.0f} kWh"
        if dollars:
            tail += f" ≈ ${dollars:,.2f}"
        tail += "). Either the entered share is slightly off, or GMP allocated from a base "
        tail += "that appears on neither bill — worth a look."
        out["note"] = head + tail
    else:
        out["note"] = (f"GMP's allocation ({offtaker_excess:,.0f} kWh) matches this offtaker's "
                       f"share of the array's group excess ({expected:,.0f} kWh).")
    return out


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

    # Share% per array (fraction, e.g. 0.2553) for the allocation cross-check.
    share_by_array: dict[int, float] = (
        {a["array_id"]: a["allocation_pct"] for a in allocs} if allocs
        else ({sub.array_id: sub.allocation_pct}
              if (getattr(sub, "array_id", None) is not None and sub.allocation_pct)
              else {}))
    array_bill_by_aid: dict[int, Optional[Bill]] = {}

    rows = []
    for aid in aids:
        arr = db.get(Array, aid)
        our_kwh = our_by_array.get(aid)
        bill = _bill_for_array_period(db, aid, start, end)
        array_bill_by_aid[aid] = bill
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

    # Anna/Bruce's allocation cross-check (share × array-total vs GMP's own credit).
    inv_label = (match.computed_invoice or {}).get("month") if match is not None else None
    try:
        allocation = _allocation_check(
            db, sub, aids, share_by_array, array_bill_by_aid, inv_label)
    except Exception:  # never let the cross-check break the reconciliation report
        allocation = {"status": "error",
                      "note": "Allocation cross-check could not run for this offtaker."}

    return {
        "sub_id": sub.id,
        "customer_name": sub.customer_name,
        "from_workbook": bool(getattr(sub, "source_workbook", None)),
        "invoice_period": (f"{start.isoformat()} → {end.isoformat()}"
                           if (start and end) else None),
        "arrays": rows,
        "overall_status": overall,
        # Per-offtaker GMP-allocation audit — flags when the excess GMP credited
        # this offtaker doesn't match their share of the array's group excess.
        "allocation": allocation,
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
    alloc_counts: dict[str, int] = {}
    alloc_dollars = 0.0
    for r in results:
        counts[r["overall_status"]] = counts.get(r["overall_status"], 0) + 1
        a = r.get("allocation") or {}
        ast = a.get("status", "n/a")
        alloc_counts[ast] = alloc_counts.get(ast, 0) + 1
        if ast == "mismatch" and a.get("delta_dollars"):
            alloc_dollars += float(a["delta_dollars"])
    return {
        "ok": True,
        "subscription_count": len(results),
        "status_counts": counts,
        # How many offtakers GMP's allocation cross-check flagged, + $ in play.
        "allocation_counts": alloc_counts,
        "allocation_flagged": alloc_counts.get("mismatch", 0),
        "allocation_dollars_flagged": round(alloc_dollars, 2),
        "subscriptions": results,
    }
