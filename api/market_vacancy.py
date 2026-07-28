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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

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


def _bill_credit_rate(db, bill, host_account_id: int, *,
                      host_account=None, array=None) -> float:
    """The $/kWh to value retained excess at, honestly: the bill's own stated
    credited-line rate → the host account's cashing history → the fleet reference
    → DEFAULT_CREDIT_RATE. Never a fabricated flat default when a real one exists.

    `host_account` / `array` let a caller hand in rows it already holds (the
    tenant-wide prefetch, or `array_vacancy`'s own host lookup) so the fleet
    fallback stops re-`get`ting them once per bill. Neither can change the
    answer: `host_account` is only ever passed the row this `db.get` returns,
    and `array` is IGNORED unless its id matches the account's `array_id` — the
    exact row the `db.get(Array, ...)` below would have produced."""
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
    acct = host_account if host_account is not None else db.get(UtilityAccount, host_account_id)
    if not (acct and acct.array_id):
        arr = None
    elif array is not None and getattr(array, "id", None) == acct.array_id:
        arr = array
    else:
        arr = db.get(Array, acct.array_id)
    provider = (acct.provider if acct else None)
    ped = bill.period_end.date() if isinstance(bill.period_end, datetime) else bill.period_end
    age = array_age_bucket(arr.first_connect_date if arr else None, ped)
    if provider:
        r = _fleet_credit_rate(db, provider=provider, age_bucket=age)
        if r is not None:
            return round(float(r), 6)
    return round(DEFAULT_CREDIT_RATE, 6)


def _registry_allocated_frac(db, array_id: int, *, subs=None) -> Optional[float]:
    """Σ(array_share_pct ?? allocation_pct) over the array's ENABLED offtaker
    subscriptions — the registry-side view of how much of the array is spoken for.
    None when no enabled subscription references this array (registry can't speak).

    `subs` accepts rows the tenant-wide prefetch already fetched (an EMPTY list
    is a real answer — "no enabled subscriptions" — and returns None just as the
    query would). The summation below stays the one and only place this number
    is computed, either way."""
    from .models import BillingReportSubscription
    if subs is None:
        subs = db.execute(
            # COLUMN DIET (enumerated, not guessed). This function reads exactly
            # TWO attributes off a subscription, but BillingReportSubscription is
            # a worse data sponge than Bill: it carries `source_workbook`
            # (LargeBinary — the ENTIRE uploaded billing workbook, persisted
            # in-row because Railway's disk is ephemeral) plus `parsed_map`, and
            # the full-entity load dragged that workbook across the wire for
            # every offtaker of every array on every /vacancy call.
            select(BillingReportSubscription.array_share_pct,
                   BillingReportSubscription.allocation_pct).where(
                BillingReportSubscription.array_id == array_id,
                BillingReportSubscription.enabled.is_(True),
            # DETERMINISM: float addition is not associative, so the row order of
            # an unordered SELECT is faintly load-bearing on the 4-dp rounding
            # the caller applies. Same class as the `Bill.id.desc()` tiebreaker
            # in energy-history; costs nothing, removes a source of drift.
            ).order_by(BillingReportSubscription.id)
        ).all()
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


# ── tenant-wide prefetch (kills the per-array N+1) ────────────────────────────

@dataclass(frozen=True)
class VacancyPrefetch:
    """Every row `array_vacancy` would otherwise fetch one array at a time,
    fetched ONCE for a whole tenant. FOUR queries replace four-per-array.

    Each map is COMPLETE for `array_ids` by construction — built from a single
    query over exactly that id set — so a MISSING KEY IS THE REAL ANSWER ("this
    array has no utility account" / "no enabled subscriptions" / "no host bill in
    the window"), never a cache miss to paper over. That is what makes this safe
    on a path that prices real offtaker invoices: there is no staleness window
    and no invalidation to get wrong, because the prefetch is built and consumed
    inside one `tenant_vacancy` call and dies with it.

    `covers()` makes the contract checkable rather than assumed: `array_vacancy`
    ignores a prefetch that wasn't built for the array in front of it and falls
    back to the single-array queries, so a mismatched prefetch can only ever cost
    queries — it can never quietly answer with another array's numbers."""
    array_ids: frozenset
    host_id_by_array: dict
    account_by_id: dict
    subs_by_array: dict
    bills_by_account: dict

    def covers(self, array_id) -> bool:
        return array_id in self.array_ids


def _prefetch_vacancy(db, arrays, *, window_months: int = VACANCY_WINDOW_MONTHS) -> VacancyPrefetch:
    """The four tenant-wide queries behind `VacancyPrefetch`.

    A prod-shaped fleet (46 arrays) issued 232 SELECTs for one /vacancy — five
    per array (host lookup, registry lookup, host-account get, the bills SELECT,
    and the host/array gets inside `_bill_credit_rate`). Every one is a cheap
    indexed single-row read, so this is round-trip cost, not scan cost — but
    /vacancy gates all four Invoices subtabs plus the default Marketplace subtab,
    so 230 sequential round trips is the whole latency.

    Each query below is the SAME predicate set as the per-array query it
    replaces, widened from `= id` to `IN (ids)`:

      1. host account per array. `_host_account_id` is `ORDER BY id LIMIT 1`
         over `array_id = X`; `MIN(id) GROUP BY array_id` returns that identical
         row (id is a non-null PK). Deliberately carries NO tenant filter, exactly
         as `_host_account_id` never had one — adding one here would be a
         behaviour change smuggled in as an optimisation.
      2. the host-account rows themselves, on a column diet: `provider` and
         `array_id` are the only attributes anything downstream reads, and
         UtilityAccount carries `service_address` + `extra` (a provider-specific
         raw JSON blob) that nothing here touches.
      3. enabled subscriptions for all arrays at once, partitioned by array_id.
      4. host bills for all host accounts at once, partitioned by account_id.
         Partitioning preserves per-account order because the ORDER BY is a TOTAL
         order (period_end, then the unique id) — a stable split of a totally
         ordered sequence keeps each key's relative order exactly.
    """
    from .models import UtilityAccount, BillingReportSubscription, Bill

    array_ids = [a.id for a in arrays]
    host_id_by_array: dict = {}
    account_by_id: dict = {}
    subs_by_array: dict = {}
    bills_by_account: dict = {}

    if array_ids:
        for aid, hid in db.execute(
            select(UtilityAccount.array_id, func.min(UtilityAccount.id))
            .where(UtilityAccount.array_id.in_(array_ids))
            .group_by(UtilityAccount.array_id)
        ).all():
            if hid is not None:
                host_id_by_array[aid] = hid

        for row in db.execute(
            select(BillingReportSubscription.array_id,
                   BillingReportSubscription.array_share_pct,
                   BillingReportSubscription.allocation_pct)
            .where(BillingReportSubscription.array_id.in_(array_ids),
                   BillingReportSubscription.enabled.is_(True))
            .order_by(BillingReportSubscription.array_id,
                      BillingReportSubscription.id)   # see _registry_allocated_frac
        ).all():
            subs_by_array.setdefault(row.array_id, []).append(row)

    host_ids = sorted(host_id_by_array.values())
    if host_ids:
        for acct in db.execute(
            select(UtilityAccount).options(
                load_only(UtilityAccount.provider, UtilityAccount.array_id)
            ).where(UtilityAccount.id.in_(host_ids))
        ).scalars().all():
            account_by_id[acct.id] = acct

        since = _now() - timedelta(days=int(window_months) * 31 + 5)
        for b in db.execute(
            # Same column diet as the single-array query below, plus `account_id`
            # — we now partition on it, and leaving it out of load_only would
            # lazy-load it per bill and rebuild the N+1 we came here to kill.
            select(Bill).options(
                load_only(Bill.account_id, Bill.kwh_sent_to_grid, Bill.raw_json,
                          Bill.solar_credit_usd, Bill.period_end)
            ).where(
                Bill.account_id.in_(host_ids),
                Bill.period_end.isnot(None),
                Bill.period_end >= since,
                Bill.kwh_sent_to_grid.isnot(None),
                Bill.kwh_sent_to_grid > 0,
            ).order_by(Bill.period_end.desc(), Bill.id.desc())
        ).scalars().all():
            bills_by_account.setdefault(b.account_id, []).append(b)

    return VacancyPrefetch(
        array_ids=frozenset(array_ids),
        host_id_by_array=host_id_by_array,
        account_by_id=account_by_id,
        subs_by_array=subs_by_array,
        bills_by_account=bills_by_account,
    )


# ── per-array vacancy ─────────────────────────────────────────────────────────

def array_vacancy(db, array, *, window_months: int = VACANCY_WINDOW_MONTHS,
                  prefetch: Optional[VacancyPrefetch] = None) -> Optional[dict]:
    """Trailing-window vacancy for ONE array. Returns a JSON-friendly dict, or None
    when the array has no host account at all (nothing to measure).

    `prefetch` is the tenant-wide fetch `tenant_vacancy` builds; without it (the
    /exchange/demand/{id}/draft-offtaker path) this issues its own queries exactly
    as it always has. Both routes run the same arithmetic on the same rows."""
    from .models import Bill

    array_id = array.id
    pre = prefetch if (prefetch is not None and prefetch.covers(array_id)) else None

    if pre is not None:
        host_id = pre.host_id_by_array.get(array_id)
        reg_alloc = _registry_allocated_frac(
            db, array_id, subs=pre.subs_by_array.get(array_id, []))
    else:
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
    host = (pre.account_by_id.get(host_id) if pre is not None
            else db.get(UtilityAccount, host_id))
    base["provider"] = (host.provider if host else None)

    if pre is not None:
        bills = pre.bills_by_account.get(host_id, [])
    else:
        since = _now() - timedelta(days=int(window_months) * 31 + 5)
        bills = db.execute(
            # COLUMN DIET (enumerated, not guessed). Bill is a data sponge: it also
            # carries `pdf_bytes` (LargeBinary — the ENTIRE bill PDF, persisted
            # in-row) and `raw_text`, and a full-entity load dragged both across the
            # wire for every bill of every array (46 queries × 479 rows = 1.29s on
            # ten_ford_demo_100) even though nothing here reads them.
            # Everything this function and its callees touch on a Bill row:
            #   kwh_sent_to_grid  — the pool
            #   raw_json          — split_excess_line_items and, inside
            #                       _bill_credit_rate, excess_credit_rate_from_bill
            #   solar_credit_usd  — the banked-month test
            #   period_end        — expiry math and the age bucket
            # (id comes along automatically as the PK.) _bill_credit_rate reads
            # NOTHING else off the bill. If a future edit does touch another column
            # SQLAlchemy loads it lazily — an extra query, never a wrong number.
            select(Bill).options(
                load_only(Bill.kwh_sent_to_grid, Bill.raw_json,
                          Bill.solar_credit_usd, Bill.period_end)
            ).where(
                Bill.account_id == host_id,
                Bill.period_end.isnot(None),
                Bill.period_end >= since,
                Bill.kwh_sent_to_grid.isnot(None),
                Bill.kwh_sent_to_grid > 0,
            # DETERMINISM: `period_end DESC` alone is NOT a total order, and the
            # `bills[:window_months]` slice below then picks an arbitrary winner
            # among tied months — the same latent nondeterminism the energy-history
            # `Bill.id.desc()` fix closed, on a path that prices invoices. The
            # prefetch orders identically, so both routes see one fixed sequence.
            ).order_by(Bill.period_end.desc(), Bill.id.desc())
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

    # ⚠️ TWO KNOWN ISSUES IN THE LOOP BELOW, both PRE-EXISTING and both left alone
    # here on purpose — they move real dollars, so they are Ford's call, not a
    # side effect of a latency patch (2026-07-28 audit, SHARED-BACKLOG):
    #
    #   1. A DUPLICATED MONTH IS COUNTED TWICE. Nothing dedupes bills by period,
    #      and prod carries 1,137 (account_id, period_end) groups with more than
    #      one excess bill — every one a RE-CAPTURE of the same month (same kWh,
    #      pulled twice). Each copy adds its own pool/retained/value, inflating
    #      that array's vacancy. The fix is to keep the freshest capture per
    #      period, which LOWERS vacancy_usd on affected arrays.
    #   2. `credit_rate` is `last_rate` — the rate of the LAST bill iterated,
    #      i.e. the OLDEST month in the window, not the newest. Almost certainly
    #      not what "the credit rate" is meant to mean on the dashboard.
    #
    # Both are why the bills query above is pinned with `Bill.id.desc()`: until a
    # dedupe exists, ties are real, and an unpinned ORDER BY let the planner pick.
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
        rate = _bill_credit_rate(db, b, host_id, host_account=host, array=array)
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
        # COLUMN DIET (enumerated, not guessed). Everything this module touches on
        # an Array row:
        #   excluded           — the skip test just below
        #   name               — array_vacancy's "array_name"
        #   first_connect_date — the age bucket, inside _bill_credit_rate
        # (id comes along as the PK, and is what `covers()` and the prefetch maps
        # key on.) Nothing here reads anything else.
        #
        # This one is not merely wire bytes: `Array.solaredge_api_key` is an
        # EncryptedStr, so the full-entity load ran an AES DECRYPT PER ARRAY on
        # every /vacancy call — and hydrated a vendor secret into a read path
        # that has no business holding one. A future edit that does touch another
        # column gets a lazy load: an extra query, never a wrong number.
        select(Array).options(
            load_only(Array.name, Array.excluded, Array.first_connect_date)
        ).where(Array.tenant_id == tenant_id)
        .order_by(Array.id)
    ).scalars().all()

    # Build the tenant-wide prefetch from the arrays we will actually measure —
    # excluded arrays are skipped below, so there is no reason to drag their host
    # bills across the wire.
    live = [a for a in arrays if not getattr(a, "excluded", False)]
    pre = _prefetch_vacancy(db, live, window_months=window_months)

    rows = []
    for a in live:
        v = array_vacancy(db, a, window_months=window_months, prefetch=pre)
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
