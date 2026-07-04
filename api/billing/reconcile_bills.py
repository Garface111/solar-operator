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

# What a confirmed catch is WORTH: GMP credits $25 per billing error they made
# (Ford, 2026-07-03: "GMP gives you twenty five dollars per failed math") — so
# the stake on a flagged bill is $25, not the kWh-delta dollars (which is just
# the size of the mis-allocation, often cents). Surfaced per row and summed.
GMP_BILLING_ERROR_CREDIT_USD = 25.0


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
    billing-model problem before trusting the number.

    Returns (reason, is_genuine): `reason` is None for a clean match; `is_genuine`
    is True ONLY when both sides have real data and the delta is a real
    production/billing difference — False for every data-quality artifact (no
    metered reads, prorated estimate, timing gap). The caller uses is_genuine to
    keep artifacts out of the operator's "needs review" count.

      • no_bill        → no captured GMP bill linked to this array yet.
      • no_invoice_data→ our invoice produced no array kWh for the period.
      • mismatch       → distinguish (a) ESTIMATE: our production is a prorated
                         bill smear (no real meter reads in the window) from
                         (b) TIMING: the bill period and invoice period don't
                         overlap, from (c) a real per-array delta.
    """
    if status == "match":
        return None, False
    if status == "no_bill":
        return "No captured GMP bill linked to this array yet — awaiting utility data.", False
    if status == "no_invoice_data":
        return "Our invoice produced no kWh for this array over the period.", False

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
                    "discrepancy."), False

    if metered == 0 and prorated > 0:
        return ("Our figure is a prorated bill estimate (no metered daily reads "
                "in this window yet) — expect drift vs. the bill total until real "
                "reads land."), False
    if metered == 0 and prorated == 0:
        return ("No daily generation rows for this array in the period — our kWh "
                "comes from the invoice fallback, not measured reads."), False
    if prorated > 0:
        return ("Period mixes measured reads with prorated bill estimates — "
                "partial data, so some drift is expected."), False
    return ("Both sides have real data — this delta reflects a genuine "
            "production/billing difference worth a closer look."), True


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
        # The stake on a confirmed catch is GMP's $25 billing-error credit —
        # not the kWh-delta dollars (that's just the mis-allocation's size).
        out["at_stake_usd"] = GMP_BILLING_ERROR_CREDIT_USD
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
        tail += ("that appears on neither bill — worth a look: GMP credits "
                 f"${GMP_BILLING_ERROR_CREDIT_USD:,.0f} per billing error they made.")
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
    # Is the invoice billed on the array's EXCESS sent to grid (GMP/VEC utility-bill
    # path — our_kwh = kwh_sent_to_grid) rather than GROSS generation? If so we must
    # compare like-for-like against the bill's EXCESS, not its gross kwh_generated,
    # or every GMP offtaker reads a permanent false "Differs from the GMP bill"
    # (7,000 excess vs 10,000 gross ≈ -30%). See audit #14 and delivery.py
    # (kwh_source == "utility_bill" / billing_basis in real_math|gmp_credited).
    excess_basis = False
    if match is not None:
        ci = match.computed_invoice or {}
        excess_basis = (
            ci.get("kwh_source") == "utility_bill"
            or ci.get("billing_basis") in ("real_math", "gmp_credited")
        )
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
    # Prefer the dedicated array_share_pct (the GMP allocation share, entered at
    # setup) over allocation_pct — an offtaker billed off their OWN already-
    # allocated bill has allocation_pct≈1.0 but a real array share <1.0. Multi-
    # array offtakers keep their per-array allocation_pct (array_share_pct is a
    # single whole-offtaker value that doesn't split per array).
    _single_share = getattr(sub, "array_share_pct", None) or sub.allocation_pct
    share_by_array: dict[int, float] = (
        {a["array_id"]: a["allocation_pct"] for a in allocs} if allocs
        else ({sub.array_id: _single_share}
              if (getattr(sub, "array_id", None) is not None and _single_share)
              else {}))
    array_bill_by_aid: dict[int, Optional[Bill]] = {}

    rows = []
    for aid in aids:
        arr = db.get(Array, aid)
        our_kwh = our_by_array.get(aid)
        bill = _bill_for_array_period(db, aid, start, end)
        array_bill_by_aid[aid] = bill
        if excess_basis:
            # our_kwh is the invoice EXCESS (kwh_sent_to_grid) — compare against the
            # bill's EXCESS, not its GROSS kwh_generated. _array_group_excess already
            # prefers kwh_sent_to_grid (falling back to net generated only when an
            # older parse never captured it), the same pool the allocation check uses.
            gmp_kwh = _array_group_excess(bill)
        else:
            gmp_kwh = float(bill.kwh_generated) if (bill and bill.kwh_generated is not None) else None
        status, delta, pct = _verdict(our_kwh, gmp_kwh)
        reason, genuine = _mismatch_reason(db, aid, status, start, end, bill)
        # A "mismatch" whose only cause is missing/estimated measured data (no
        # metered reads, a prorated bill smear, or a period-timing gap) is NOT a
        # real billing discrepancy — it's "we can't verify this against the bill
        # yet". Reclassify it to `unverified` so it never inflates the operator's
        # "needs review" count. Only a real delta with data on BOTH sides stays
        # `mismatch`. (Fixes the 104-false-alarm: every offtaker flagged only
        # because the fleet has no measured generation yet.)
        if status == "mismatch" and not genuine:
            status = "unverified"
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
            "mismatch_reason": reason,
            "gmp_bill_period": bp,
            "gmp_total_cost": (float(bill.total_cost) if (bill and bill.total_cost is not None) else None),
        })

    # Roll up: a GENUINE mismatch dominates; then any clean match; then
    # `unverified` (data-quality artifact, awaiting measured data — NOT a review
    # item); then no_bill; else no_invoice_data.
    statuses = {r["status"] for r in rows}
    if not rows:
        overall = "no_arrays"
    elif "mismatch" in statuses:
        overall = "mismatch"
    elif "match" in statuses:
        overall = "match" if statuses == {"match"} else "partial"
    elif "unverified" in statuses:
        overall = "unverified"
    elif "no_bill" in statuses:
        overall = "no_bill"
    else:
        overall = "no_invoice_data"

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


# ── Generation-time cross-check (Bruce, 2026-07) ────────────────────────────
# Share-variance flag threshold, in PERCENTAGE POINTS: GMP's implied share for
# the offtaker (their credited excess ÷ the array bill's group excess) vs the
# "Their share of the array (%)" the operator entered. Bruce: "You should pick
# a threshold for the variance flag. Maybe .1%? This would be a good discussion
# point with Anna." — so it lives here as ONE named constant, ready to retune
# after that conversation.
SHARE_VARIANCE_THRESHOLD_PCT = 0.1


def generation_crosscheck(db: Session, sub: BillingReportSubscription) -> Optional[dict]:
    """Bruce's automatic invoice-time cross-check: runs in the background when an
    invoice draft is generated and surfaces WITH it — no button, no extra tab.

    Verifies GMP's own numbers against the operator's inputs:
      • share — the share GMP effectively used (credited ÷ the array bill's
        group excess) vs the entered share, flagged beyond
        SHARE_VARIANCE_THRESHOLD_PCT percentage points;
      • kWh — the credited excess vs share × the array bill's pool, flagged
        beyond the same _ALLOC_TOL_KWH the audit surfaces use (this is what
        catches Bruce's real worked example, where the share variance itself
        is only ~0.009 points).
    `flagged` is the OR of the two, so the invoice-time check is never weaker
    than the audit sandbox. Per Bruce there is deliberately NO rate-vs-published-
    schedule comparison here — the bill's own scraped credit rate is the billing
    truth (the GMP schedule stays a passive reference on the setup form).

    Reuses reconcile_subscription — the SAME engine behind /reconcile-bills and
    the audit sandbox — so the numbers can never drift between surfaces. Fail-
    soft BY DESIGN: any state where the check can't run honestly (no settled
    bill, no entered share, single-meter setup, no offtaker account/bill)
    returns None; generation is never blocked and no verdict is fabricated.
    """
    try:
        rep = reconcile_subscription(db, sub)
    except Exception:  # noqa: BLE001 — the cross-check must never break generation
        return None
    alloc = (rep or {}).get("allocation") or {}
    if alloc.get("status") not in ("match", "mismatch"):
        return None                       # honest can't-run states → no verdict
    credited = alloc.get("offtaker_credited_kwh")
    expected = alloc.get("expected_kwh")
    master = alloc.get("array_group_excess_kwh")
    if credited is None or expected is None or not master:
        return None
    computed_share = float(credited) / float(master) * 100.0
    per_array = alloc.get("per_array") or []
    if len(per_array) == 1 and per_array[0].get("share"):
        # Single-array (the normal case): show the exact share the operator typed.
        entered_share = float(per_array[0]["share"]) * 100.0
    else:
        # Multi-array: the effective blended share the entered values imply.
        entered_share = float(expected) / float(master) * 100.0
    variance = computed_share - entered_share
    flagged = (abs(variance) > SHARE_VARIANCE_THRESHOLD_PCT
               or alloc.get("status") == "mismatch")
    return {
        "computed_share_pct": round(computed_share, 4),
        "entered_share_pct": round(entered_share, 4),
        "variance_pct": round(variance, 4),
        "kwh_master": round(float(master), 1),
        "kwh_offtaker_expected": round(float(expected), 1),
        "kwh_offtaker_credited": round(float(credited), 1),
        "delta_kwh": alloc.get("delta_kwh"),
        "delta_dollars": alloc.get("delta_dollars"),
        "flagged": flagged,
        "threshold_pct": SHARE_VARIANCE_THRESHOLD_PCT,
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
        # The real stake: GMP credits $25 per billing error — flagged × $25.
        "allocation_at_stake_usd": round(
            alloc_counts.get("mismatch", 0) * GMP_BILLING_ERROR_CREDIT_USD, 2),
        "subscriptions": results,
    }


def _array_provider(db: Session, array_id: int) -> Optional[str]:
    """The utility provider of the array's OWN (host) account — for the audit
    sandbox's per-utility sub-tabs (gmp / vec / smarthub / …)."""
    return db.execute(
        select(UtilityAccount.provider).where(UtilityAccount.array_id == array_id)
        .order_by(UtilityAccount.id)
    ).scalars().first()


def audit_by_array(db: Session, tenant_id: str) -> dict:
    """The "bill audit sandbox" (Anna/Bruce): organize the fleet as GMP itself
    allocates it — the ARRAY's master bill on top (its group excess), each
    offtaker's OWN bill underneath (their share, what GMP credited them, what they
    SHOULD be by the math, the delta, and a flag when GMP got it wrong), grouped
    into per-utility sub-tabs. Reuses the exact per-offtaker allocation math from
    reconcile_subscription so the sandbox and the invoice cross-check never drift.

    Returns {utilities:[{provider, arrays:[{array_id, array_name, group_excess_kwh,
    credit_rate, period, flagged, offtakers:[{sub_id, customer_name, share_pct,
    gmp_credited_kwh, should_be_kwh, delta_kwh, delta_dollars, status, note}]}]}],
    totals:{arrays, offtakers, flagged, dollars_flagged}}.
    """
    from .delivery import _normalized_allocations  # lazy — delivery imports this pkg

    subs = db.execute(
        select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tenant_id,
            BillingReportSubscription.deleted_at.is_(None))
        .order_by(BillingReportSubscription.customer_name)
    ).scalars().all()

    arrays: dict[int, dict] = {}
    total_flagged = 0
    total_dollars = 0.0

    # The audit surfaces ONLY the allocation cross-check, so skip the full
    # invoice rebuild (build_match — the ~60s-at-800-offtakers cost that made
    # the Bill audit tab load slowly, Ford 2026-07-04). Call the SAME
    # _allocation_check directly with cheap bill lookups; the array's host bill
    # is cached (every offtaker on an array reads the same one → one query per
    # array, not per offtaker). Math is identical, so the flagged set never
    # drifts from the invoice cross-check.
    host_bill_cache: dict[int, Optional[Bill]] = {}

    def _host_bill(aid: int) -> Optional[Bill]:
        if aid not in host_bill_cache:
            host_bill_cache[aid] = _bill_for_array_period(db, aid, None, None)
        return host_bill_cache[aid]

    for sub in subs:
        aid = getattr(sub, "array_id", None)
        allocs = _normalized_allocations(sub)
        if aid is None and allocs:
            aid = allocs[0]["array_id"]
        if aid is None:
            continue
        aids = ([x["array_id"] for x in allocs] if allocs
                else ([sub.array_id] if getattr(sub, "array_id", None) is not None else []))
        _single_share = getattr(sub, "array_share_pct", None) or sub.allocation_pct
        share_by_array = (
            {x["array_id"]: x["allocation_pct"] for x in allocs} if allocs
            else ({sub.array_id: _single_share}
                  if (getattr(sub, "array_id", None) is not None and _single_share)
                  else {}))
        array_bill_by_aid = {x: _host_bill(x) for x in aids}
        # Match the invoice-cross-check (reconcile) EXACTLY so audit and the KPI
        # chip never disagree: a MONTHLY offtaker is checked against their latest
        # settled bill (target_label=None → latest); a QUARTERLY offtaker's
        # invoice period is a quarter, which the per-bill credit resolver can't
        # match to a single month — reconcile leaves it "no_offtaker_bill", so
        # we pass a non-matching monthly label to reproduce that (a quarterly
        # allocation is cross-checked at quarter close, not per month).
        _lbl = ("1900-01"
                if (getattr(sub, "cadence", "monthly") == "quarterly") else None)
        try:
            a = _allocation_check(db, sub, aids, share_by_array,
                                  array_bill_by_aid, _lbl) or {}
        except Exception:  # never let one offtaker break the audit
            a = {}
        entry = arrays.get(aid)
        if entry is None:
            arr = db.get(Array, aid)
            entry = {
                "array_id": aid,
                "array_name": arr.name if arr else f"Array {aid}",
                "provider": (_array_provider(db, aid) or "other"),
                "group_excess_kwh": a.get("array_group_excess_kwh"),
                "credit_rate": a.get("credit_rate"),
                "flagged": 0,
                "offtakers": [],
            }
            arrays[aid] = entry
        # The group excess comes from the array's own bill — fill it from the first
        # offtaker that resolved it (they all read the same host bill).
        if entry["group_excess_kwh"] is None and a.get("array_group_excess_kwh") is not None:
            entry["group_excess_kwh"] = a["array_group_excess_kwh"]
        if entry["credit_rate"] is None and a.get("credit_rate") is not None:
            entry["credit_rate"] = a["credit_rate"]
        share = getattr(sub, "array_share_pct", None) or sub.allocation_pct
        flagged = a.get("status") == "mismatch"
        if flagged:
            entry["flagged"] += 1
            total_flagged += 1
            if a.get("delta_dollars"):
                total_dollars += float(a["delta_dollars"])
        entry["offtakers"].append({
            "sub_id": sub.id,
            "customer_name": sub.customer_name,
            "share_pct": round(share, 6) if share else None,
            "gmp_credited_kwh": a.get("offtaker_credited_kwh"),
            "should_be_kwh": a.get("expected_kwh"),
            "delta_kwh": a.get("delta_kwh"),
            "delta_dollars": a.get("delta_dollars"),
            "at_stake_usd": a.get("at_stake_usd"),
            "status": a.get("status"),
            "note": a.get("note"),
        })

    # Group arrays into per-utility sub-tabs.
    by_provider: dict[str, list] = {}
    for entry in arrays.values():
        by_provider.setdefault(entry["provider"], []).append(entry)
    utilities = [
        {"provider": prov, "arrays": sorted(arrs, key=lambda x: x["array_name"])}
        for prov, arrs in sorted(by_provider.items())
    ]
    total_offtakers = sum(len(e["offtakers"]) for e in arrays.values())
    return {
        "ok": True,
        "utilities": utilities,
        "totals": {
            "arrays": len(arrays),
            "offtakers": total_offtakers,
            "flagged": total_flagged,
            "dollars_flagged": round(total_dollars, 2),
            # flagged × GMP's $25 billing-error credit — the headline stake.
            "at_stake_usd": round(total_flagged * GMP_BILLING_ERROR_CREDIT_USD, 2),
        },
    }
