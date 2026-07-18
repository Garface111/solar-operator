"""
Auto-applied blended retail rate — keyed by utility × location × array-age ×
billing-month, DERIVED FROM CAPTURED BILLS (never invented).

Why this exists
---------------
Billing needs a "net rate" to apply the discount against. Rather than ask the
operator to type a rate (or hardcode a guess that goes stale every ~2 years when
VT resets net-metering rates), we:

  1. MEASURE the real blended $/kWh from the GMP bills we already capture
     (derive_blended_rate_from_bills), bucketed by utility / location / age /
     effective period, and store the result in the RateSchedule table.
  2. RESOLVE the right cell at invoice time (resolve_net_rate) by the customer's
     array: its utility, region, age (first_connect_date), and the billing month.

Every value is auditable: each RateSchedule row carries sample_size +
source_note + computed_at. Nothing here fabricates a number — if there are no
bills to measure and no schedule row, the resolver falls back to the documented
VT blended default in api/rates.py and says so via provenance.

Age rule
--------
VT's net-metering solar adder runs ~10 years; year 11+ an array drops toward the
base blended rate. We bucket by 'le11' (≤11 yrs since first_connect_date) vs
'gt11' so the measured rate reflects that step automatically — we don't hardcode
the adder, we MEASURE each bucket from real bills.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .rates import DEFAULT_RATE_USD_PER_KWH, get_energy_rate

AGE_THRESHOLD_YEARS = 11   # ≤11 vs >11 — VT 10-yr adder expiry boundary (+1 grace)

# Non-production tenants whose SEEDED bills carry invented rates (api/seed_demo.py
# rigs GMP bills at 0.140–0.176 $/kWh) and must NEVER enter the cross-tenant
# medians that price REAL invoices — _fleet_credit_rate (banked-month reference)
# and derive_blended_rate_from_bills (the RateSchedule discount basis). Both of
# these tenants carry is_demo=False — a known seed-hygiene gap (SHARED-BACKLOG
# 2026-07-16, fold-backend) — so the usual Tenant.is_demo filter alone does NOT
# catch them; we filter is_demo AND this explicit set (belt and suspenders). The
# systemic fix (marking them is_demo=True) is Ford's call: it flips
# ten_demo_realistic to read-only (see the Tenant.is_demo note in models.py).
SYNTHETIC_TENANT_IDS = ("ten_demo_realistic", "ten_ford_demo_100")


# ─── 1. Derivation from captured bills ──────────────────────────────────────

def blended_rate_from_bill(raw_json: dict) -> Optional[float]:
    """The blended retail $/kWh for ONE bill, from its line items.

    GMP bill segmentLineItems carry KWH rows; the positive-dollar NET energy
    charges over the consumed kWh give the gross retail rate the customer is
    billed (before net-metering credits, which appear as separate EXCESS/credit
    lines). Returns None if the bill has no usable charge/kWh pair.
    """
    if not isinstance(raw_json, dict):
        return None
    total_charge = 0.0
    total_kwh = 0.0
    for seg in raw_json.get("billSegments", []):
        consumed = 0.0
        for li in seg.get("segmentLineItems", []):
            if li.get("unitOfMeasure") == "KWH" and li.get("unitCode") == "CONSUMED":
                consumed = max(consumed, float(li.get("unitCount") or 0))
        seg_charge = 0.0
        for li in seg.get("segmentLineItems", []):
            da = li.get("dollarAmount")
            if (li.get("unitOfMeasure") == "KWH" and li.get("unitCode") == "NET"
                    and (da or 0) > 0):
                seg_charge += float(da)
        if consumed > 0 and seg_charge > 0:
            total_charge += seg_charge
            total_kwh += consumed
    if total_kwh <= 0:
        return None
    rate = total_charge / total_kwh
    # Guard against parse noise — a residential blended rate lives in this band.
    return rate if 0.05 < rate < 0.50 else None


# ─── 1b. Net-metering SOLAR CREDIT from a bill (offtaker model, Ford/Bruce) ───

# GMP line-item codes for the credit side of a net-metered bill (page-2 detail).
_EXCESS_CODES = {"EXCESS", "EXCESSO"}   # kWh sent to grid, credited (base credit)
_SOLCRED_CODES = {"SOLCRED"}            # solar incentive credit (added when present)

# A net-metering credit rate below this $/kWh means the month's excess was BANKED
# (rolled forward, not cashed) rather than credited at the energy rate — ignored
# for offtaker billing. Normal VT solar credit ~$0.21–0.26/kWh; banked ~$0.
BANKED_CREDIT_RATE_FLOOR = 0.05


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def solar_credit_from_bill(raw_json: dict) -> Optional[dict]:
    """The net-metering SOLAR CREDIT an array earned on ONE bill, from its page-2
    line items (Ford/Bruce's offtaker model, Jun 2026).

    An offtaker is billed for the value of the solar EXCESS sent to the grid at the
    credit the utility ACTUALLY gave — not the retail consumption rate, not a flat
    default. Sum the NEGATIVE credit lines:
        EXCESS / EXCESSO  — energy sent to grid, credited (the base credit)
        SOLCRED           — solar incentive credit (added when present)
    SOLCRED is optional → default to the excess credit alone when absent.

    Returns {excess_kwh, credit_usd, credit_rate} (credit_rate = credit_usd /
    excess_kwh, $/kWh) or None when the bill has no usable excess, no credit, or is
    a BANKED month (excess present but credited at ~$0 — rolled forward, not cashed;
    ignored for offtaker billing per Bruce).
    """
    if not isinstance(raw_json, dict):
        return None
    excess_kwh = excess_usd = solcred_usd = 0.0
    for seg in raw_json.get("billSegments", []):
        for li in seg.get("segmentLineItems", []):
            if li.get("unitOfMeasure") != "KWH":
                continue
            uc = li.get("unitCode")
            da = _f(li.get("dollarAmount"))
            cnt = _f(li.get("unitCount"))
            if uc in _EXCESS_CODES:
                if cnt:
                    excess_kwh += cnt
                if da is not None and da < 0:
                    excess_usd += -da            # credit magnitude (negatives summed)
            elif uc in _SOLCRED_CODES:
                if da is not None and da < 0:
                    solcred_usd += -da
    if excess_kwh <= 0:
        return None
    credit_usd = round(excess_usd + solcred_usd, 2)   # SOLCRED optional → excess alone
    if credit_usd <= 0:
        return None
    rate = credit_usd / excess_kwh
    if rate < BANKED_CREDIT_RATE_FLOOR:
        return None                                   # banked month — ignore
    return {"excess_kwh": round(excess_kwh, 1),
            "credit_usd": credit_usd,
            "credit_rate": round(rate, 5)}


def tenant_bill_credit_rate(db: Session, tenant_id: str, *,
                            lookback_days: int = 180,
                            max_bills: int = 80) -> dict:
    """Fleet-default solar credit rate MEASURED from the tenant's own utility bills.

    Scans recent Bill.raw_json rows (GMP + any co-op bills we can parse) and
    takes the MEDIAN of each bill's stated net-metering EXCESS credit rate
    (`excess_credit_rate_from_bill`). This is the honest default for the
    master "solar credit rate" field — NOT the Vermont tariff constant — when
    the operator hasn't typed an override.

    Returns:
      {rate, sample_size, source, note} where source is
      "utility_bills" | "none". rate is None when no bill yields a rate.
    """
    from datetime import timedelta

    from .models import Bill, UtilityAccount, now as _now

    since = _now() - timedelta(days=int(lookback_days))
    # Prefer newest bills; raw_json is the GMP segment payload. VEC bill PDFs
    # land credit_rate via solar_credit_usd/kwh when raw_json is sparse.
    rows = db.execute(
        select(Bill.raw_json, Bill.solar_credit_usd, Bill.kwh_sent_to_grid,
               Bill.kwh_generated, UtilityAccount.provider)
        .join(UtilityAccount, UtilityAccount.id == Bill.account_id)
        .where(
            Bill.tenant_id == tenant_id,
            Bill.period_end.isnot(None),
            Bill.period_end >= since,
        )
        .order_by(Bill.period_end.desc())
        .limit(max_bills)
    ).all()

    rates: list[float] = []
    for raw, credit_usd, sent, gen, provider in rows:
        r = excess_credit_rate_from_bill(raw) if isinstance(raw, dict) else None
        if r is None and isinstance(raw, dict):
            sc = solar_credit_from_bill(raw)
            if sc and sc.get("credit_rate"):
                r = float(sc["credit_rate"])
        # Fallback: stored solar_credit_usd ÷ excess kWh when raw parse failed
        # but the bill row already carries the credit dollars (VEC/PDF path).
        if r is None and credit_usd and credit_usd > 0:
            kwh = sent if (sent and sent > 0) else gen
            if kwh and kwh > 0:
                cand = float(credit_usd) / float(kwh)
                if 0.05 < cand < 0.80:   # same guard band as blended_rate
                    r = round(cand, 5)
        if r is not None and 0.05 < r < 0.80:
            rates.append(float(r))

    if not rates:
        return {
            "rate": None,
            "sample_size": 0,
            "source": "none",
            "note": "No utility-bill credit rates found yet — connect GMP (or co-op) "
                    "bills so we can read the rate each statement states.",
        }
    rates_sorted = sorted(rates)
    med = statistics.median(rates_sorted)
    n = len(rates_sorted)
    # Round like the bill lines (5 dp) for stable UI display.
    med_r = round(float(med), 5)
    note = (f"Median EXCESS credit rate from {n} of your utility bill"
            f"{'' if n == 1 else 's'} in the last {lookback_days} days "
            f"(${med_r:.5f}/kWh). Each offtaker still prices off THEIR bound "
            f"bill when it settles.")
    return {
        "rate": med_r,
        "sample_size": n,
        "source": "utility_bills",
        "note": note,
    }


def excess_credit_rate_from_bill(raw_json: dict) -> Optional[float]:
    """The per-kWh net-metering EXCESS credit RATE the bill STATES ($/kWh), read
    from the CREDITED line(s) alone — i.e. credit dollars ÷ the kWh those dollars
    credited, NOT ÷ all excess on the bill.

    Why this is separate from solar_credit_from_bill: under GMP GROUP net metering
    most of an array's excess is SHARED OUT to group members and shows on the host's
    own bill as an EXCESS line at $0 (the value left with the members, not cashed
    here). Only the small residual the host kept is a credited line (e.g. the
    screenshot's "9 Total KWH Excess Credit @ $-0.18398"). solar_credit_from_bill
    divides the residual credit across ALL excess kWh, diluting the rate to ~$0 and
    tripping the banked floor — so the offtaker falls back to a fleet reference
    estimate instead of the rate printed on the bill. This reads the RATE off the
    credited line only ($0.18398/kWh), which IS the canonical net-metering credit
    rate for the period, so the offtaker's shared excess bills at the bill's own
    rate. Returns None for a truly banked month (no line credited at a real rate)."""
    if not isinstance(raw_json, dict):
        return None
    excess_usd = solcred_usd = credited_kwh = 0.0
    for seg in raw_json.get("billSegments", []):
        for li in seg.get("segmentLineItems", []):
            if li.get("unitOfMeasure") != "KWH":
                continue
            uc = li.get("unitCode")
            da = _f(li.get("dollarAmount"))
            cnt = _f(li.get("unitCount"))
            if uc in _EXCESS_CODES:
                # Only lines that ACTUALLY credited dollars define the rate; the
                # $0 group-shared excess line is excluded from both sides.
                if da is not None and da < 0 and cnt and cnt > 0:
                    excess_usd += -da
                    credited_kwh += cnt
            elif uc in _SOLCRED_CODES:
                if da is not None and da < 0:
                    solcred_usd += -da
    if credited_kwh <= 0:
        return None
    credit_usd = excess_usd + solcred_usd            # SOLCRED optional
    if credit_usd <= 0:
        return None
    rate = credit_usd / credited_kwh
    if rate < BANKED_CREDIT_RATE_FLOOR:
        return None                                   # banked — no real credit rate
    return round(rate, 5)


def array_age_bucket(first_connect_date, as_of: Optional[date] = None) -> str:
    """'le11' if the array is ≤ AGE_THRESHOLD_YEARS old at as_of, else 'gt11'.
    Unknown install date → 'le11' (the common/newer case; conservative)."""
    if not first_connect_date:
        return "le11"
    as_of = as_of or date.today()
    fc = first_connect_date.date() if isinstance(first_connect_date, datetime) else first_connect_date
    years = (as_of - fc).days / 365.25
    return "gt11" if years > AGE_THRESHOLD_YEARS else "le11"


@dataclass
class DerivedRate:
    rate: float
    sample_size: int
    note: str


def derive_blended_rate_from_bills(
    db: Session, *, utility: str, effective_start: date, effective_end: Optional[date],
    location_class: str = "*", age_bucket: str = "*", min_samples: int = 8,
) -> Optional[DerivedRate]:
    """Measure the median blended $/kWh from captured bills matching the cell.

    Returns None when too few bills to be trustworthy (caller leaves the cell
    empty / falls back). Median is used (robust to outliers) over the bills whose
    period_end lands in [effective_start, effective_end).
    """
    from .models import Bill, UtilityAccount, Array, Tenant

    # Same demo/synthetic exclusion as _fleet_credit_rate: this median feeds the
    # RateSchedule table (the discount basis), so seeded non-production bills must
    # never enter it. See SYNTHETIC_TENANT_IDS.
    q = (select(Bill, Array.first_connect_date)
         .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
         .join(Tenant, UtilityAccount.tenant_id == Tenant.id)
         .join(Array, UtilityAccount.array_id == Array.id, isouter=True)
         .where(UtilityAccount.provider == utility,
                Tenant.is_demo.is_(False),
                UtilityAccount.tenant_id.notin_(SYNTHETIC_TENANT_IDS),
                Bill.raw_json.isnot(None),
                Bill.period_end >= effective_start))
    if effective_end is not None:
        q = q.where(Bill.period_end < effective_end)
    if location_class != "*":
        q = q.where(Array.region == location_class)

    rates: list[float] = []
    for bill, fc in db.execute(q.limit(5000)).all():
        if age_bucket != "*":
            pe = bill.period_end.date() if isinstance(bill.period_end, datetime) else bill.period_end
            if array_age_bucket(fc, pe) != age_bucket:
                continue
        r = blended_rate_from_bill(bill.raw_json)
        if r is not None:
            rates.append(r)
    if len(rates) < min_samples:
        return None
    med = round(statistics.median(rates), 5)
    return DerivedRate(rate=med, sample_size=len(rates),
                       note=f"median of {len(rates)} captured {utility.upper()} bills "
                            f"{effective_start:%Y-%m}–{(effective_end or date.today()):%Y-%m}")


# ─── 2. Resolution at invoice time ──────────────────────────────────────────

@dataclass
class ResolvedRate:
    rate: float
    source: str   # 'schedule' | 'schedule_provisional' | 'vt_default'
    note: str


def resolve_net_rate(
    db: Session, *, provider: Optional[str], region: Optional[str],
    first_connect_date, period_end: Optional[date],
) -> ResolvedRate:
    """The auto-applied blended net rate for an array's billing period.

    Looks up the RateSchedule row whose effective window contains period_end,
    matching utility + location + age, preferring the MOST SPECIFIC match and
    narrowing gracefully (exact age/region → wildcards). Falls back to the
    documented VT blended default (api/rates.py) when no row matches — never
    fabricates. Always returns a provenance note.
    """
    from .models import RateSchedule

    util = (provider or "*").strip().lower()
    as_of = period_end or date.today()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    age = array_age_bucket(first_connect_date, as_of)
    loc = (region or "*").strip().lower()

    rows = db.execute(
        select(RateSchedule).where(
            RateSchedule.effective_start <= as_of,
        )
    ).scalars().all()

    def in_window(r) -> bool:
        return r.effective_end is None or r.effective_end > as_of

    # Specificity-ranked candidate keys (utility, location, age).
    candidates = [
        (util, loc, age), (util, loc, "*"), (util, "*", age), (util, "*", "*"),
        ("*", loc, age), ("*", "*", age), ("*", "*", "*"),
    ]
    for (u, l, a) in candidates:
        best = None
        for r in rows:
            if not in_window(r):
                continue
            if (r.utility.lower() == u and r.location_class.lower() == l
                    and r.age_bucket == a):
                # Among matches, newest effective_start wins.
                if best is None or r.effective_start > best.effective_start:
                    best = r
        if best is not None:
            src = "schedule_provisional" if best.is_provisional else "schedule"
            tag = " (provisional)" if best.is_provisional else ""
            return ResolvedRate(
                rate=float(best.blended_rate_per_kwh), source=src,
                note=(f"VT blended · {best.utility.upper()} · age {best.age_bucket} · "
                      f"eff {best.effective_start:%Y-%m}"
                      f"{'–' + format(best.effective_end, '%Y-%m') if best.effective_end else '+'}"
                      f"{tag}"))

    # No schedule row → documented provider default (never invented here).
    rate = get_energy_rate(provider)
    return ResolvedRate(rate=rate, source="vt_default",
                        note=f"VT blended default ({provider or 'unknown'} → ${rate:.3f}/kWh)")


# ─── 3. Refresh: (re)compute the schedule from captured bills ────────────────

# VT net-metering rate periods reset ~every 2 years. These are the EFFECTIVE
# WINDOWS we bucket bills into; the rate VALUE for each is MEASURED, not set.
# Add the next window here (or via the admin endpoint) when the biennial reset
# lands — the resolver auto-picks it once billing months roll in.
DEFAULT_WINDOWS: list[tuple[date, Optional[date]]] = [
    (date(2022, 1, 1), date(2024, 1, 1)),
    (date(2024, 1, 1), date(2026, 1, 1)),
    (date(2026, 1, 1), None),            # current, open-ended
]


def refresh_rate_schedule(
    db: Session, *, utilities: Optional[list[str]] = None,
    windows: Optional[list[tuple[date, Optional[date]]]] = None,
    min_samples: int = 8,
) -> dict:
    """Recompute RateSchedule cells from captured bills and upsert them.

    For each (utility × effective-window × age_bucket) it measures the median
    blended rate from real bills and writes/updates one row. Cells with too few
    bills are skipped (left to fall back). Idempotent — safe to run on a cadence
    or from the admin endpoint. Returns a summary of what was written.
    """
    from .models import RateSchedule, UtilityAccount

    windows = windows or DEFAULT_WINDOWS
    if utilities is None:
        utilities = [u for (u,) in db.execute(
            select(UtilityAccount.provider).distinct()).all() if u]

    written, skipped = 0, 0
    details: list[dict] = []
    for util in utilities:
        for (start, end) in windows:
            for age in ("le11", "gt11"):
                d = derive_blended_rate_from_bills(
                    db, utility=util, effective_start=start, effective_end=end,
                    age_bucket=age, min_samples=min_samples)
                if d is None:
                    skipped += 1
                    continue
                existing = db.execute(select(RateSchedule).where(
                    RateSchedule.state == "VT", RateSchedule.utility == util,
                    RateSchedule.location_class == "*", RateSchedule.age_bucket == age,
                    RateSchedule.effective_start == start)).scalar_one_or_none()
                if existing:
                    existing.effective_end = end
                    existing.blended_rate_per_kwh = d.rate
                    existing.sample_size = d.sample_size
                    existing.source_note = d.note
                    existing.is_provisional = False
                    existing.computed_at = datetime.utcnow()
                else:
                    db.add(RateSchedule(
                        state="VT", utility=util, location_class="*", age_bucket=age,
                        effective_start=start, effective_end=end,
                        blended_rate_per_kwh=d.rate, sample_size=d.sample_size,
                        source_note=d.note, is_provisional=False))
                written += 1
                details.append({"utility": util, "age": age,
                                "eff": f"{start:%Y-%m}", "rate": d.rate,
                                "n": d.sample_size})
    db.commit()
    return {"written": written, "skipped": skipped, "cells": details}


# ─── 4. Offtaker credit resolution (option B: bill banked months too) ─────────

# Final fallback net-metering CREDIT rate ($/kWh) when neither the bill, the
# account's own history, nor the fleet supplies one. The validated VT solar credit
# (EXCESS + SOLCRED) for newer arrays — see solar_credit_from_bill validation.
DEFAULT_CREDIT_RATE = 0.2576


def _median(vals: list[float]) -> Optional[float]:
    return round(statistics.median(vals), 5) if vals else None


# A single mis-parsed bill (e.g. a $2,000 credit read onto a 10 kWh line = $200/kWh)
# would otherwise skew a small median. A real cashed VT net-metering credit rate
# lives in this band — outside it is parse noise (or a banked ~$0 month that slipped
# the credit>0 filter). Mirrors blended_rate_from_bill's own 0.05–0.50 guard.
CREDIT_RATE_LO, CREDIT_RATE_HI = 0.05, 0.50


def _sane_credit_rate(r: float) -> bool:
    return CREDIT_RATE_LO < r < CREDIT_RATE_HI


def _account_credit_rate(db, utility_account_id: int) -> Optional[float]:
    """Median net-metering credit rate ($/kWh) over an account's CASHED months
    (solar_credit_usd > 0). None when the account has never cashed a credit."""
    from .models import Bill
    rows = db.execute(
        select(Bill.solar_credit_usd, Bill.kwh_sent_to_grid).where(
            Bill.account_id == utility_account_id,
            Bill.solar_credit_usd.isnot(None), Bill.solar_credit_usd > 0,
            Bill.kwh_sent_to_grid.isnot(None), Bill.kwh_sent_to_grid > 0)
    ).all()
    rates = [float(c) / float(k) for c, k in rows if k]
    return _median([r for r in rates if _sane_credit_rate(r)])


def _fleet_credit_rate(db, *, provider: str, age_bucket: str,
                       min_samples: int = 8) -> Optional[float]:
    """Median credit rate across the fleet's CASHED bills in this provider + age
    cell (so a never-cashing account is valued like its peers). None if too few.

    Excludes non-production (demo/synthetic) tenants — their seeded bills carry
    invented rates that would poison the reference median used to price REAL
    banked-month offtaker invoices. See SYNTHETIC_TENANT_IDS."""
    from .models import Bill, UtilityAccount, Array, Tenant
    q = (select(Bill.solar_credit_usd, Bill.kwh_sent_to_grid,
                Array.first_connect_date, Bill.period_end)
         .join(UtilityAccount, Bill.account_id == UtilityAccount.id)
         .join(Tenant, UtilityAccount.tenant_id == Tenant.id)
         .join(Array, UtilityAccount.array_id == Array.id, isouter=True)
         .where(UtilityAccount.provider == provider,
                Tenant.is_demo.is_(False),
                UtilityAccount.tenant_id.notin_(SYNTHETIC_TENANT_IDS),
                Bill.solar_credit_usd.isnot(None), Bill.solar_credit_usd > 0,
                Bill.kwh_sent_to_grid.isnot(None), Bill.kwh_sent_to_grid > 0)
         # DETERMINISM (found 2026-07-02): prod has >20k qualifying bills, and
         # an UNORDERED limit let Postgres return an arbitrary subset per call —
         # the fleet median (and thus every reference-rate invoice amount)
         # drifted ~0.4% between two builds of the SAME period. Newest-first
         # makes the sample stable AND most representative of current rates.
         .order_by(Bill.period_end.desc(), Bill.id.desc()))
    rates = []
    for cu, k, fc, pe in db.execute(q.limit(20000)).all():
        if not k:
            continue
        ped = pe.date() if isinstance(pe, datetime) else pe
        if array_age_bucket(fc, ped) != age_bucket:
            continue
        r = float(cu) / float(k)
        if _sane_credit_rate(r):
            rates.append(r)
    return _median(rates) if len(rates) >= min_samples else None


def resolve_offtaker_excess_credit(db, utility_account_id: int, target_label: Optional[str] = None):
    """OPTION B offtaker billing basis: the latest period's EXCESS sent to grid,
    valued at the net-metering credit rate.

    CASHED month → the bill's own rate (solar_credit_usd ÷ excess). BANKED month
    (solar_credit_usd NULL/0) → a REFERENCE rate so the offtaker still pays for the
    solar they received while the host keeps the banked credit (trued up annually):
    the account's own cashing history → the fleet median for the array's age bucket
    → DEFAULT_CREDIT_RATE.

    Returns (excess_kwh, credit_usd, credit_rate, period_start, period_end, label,
    rate_source) — rate_source ∈ {'bill_cash','reference'} — or None when the latest
    bill has no excess to bill.
    """
    from .models import Bill, UtilityAccount, Array
    bills = db.execute(
        select(Bill).where(
            Bill.account_id == utility_account_id,
            Bill.kwh_sent_to_grid.isnot(None), Bill.kwh_sent_to_grid > 0,
            Bill.period_end.isnot(None))
        .order_by(Bill.period_end.desc())
    ).scalars().all()
    # Default: the latest billed period. With target_label ("YYYY-MM"), pick THAT
    # period's bill instead — so the same canonical math can value any historical
    # period (used to backfill an offtaker's generation spreadsheet). DB-agnostic
    # match in Python (few bills per account).
    def _lbl(b):
        pe = b.period_end.date() if isinstance(b.period_end, datetime) else b.period_end
        return pe.strftime("%Y-%m") if pe else None
    if target_label is not None:
        bill = next((b for b in bills if _lbl(b) == target_label), None)
    else:
        bill = bills[0] if bills else None
    if bill is None:
        return None
    excess = round(float(bill.kwh_sent_to_grid), 1)
    # 1) Fully-cashed bill (solar_credit_usd captured) → its own cash rate.
    # 2) Else the rate the bill STATES on its credited excess line — the default the
    #    operator expects ("the credit rate in the bill"). This is what rescues GROUP
    #    net-metering offtakers, whose excess is shared out at $0 so solar_credit_usd
    #    never gets captured (kwh_sent_to_grid is the shared amount; the rate lives on
    #    the small residual credited line). Only when the bill states NO real credited
    #    rate (a truly banked month) do we fall back to a reference estimate.
    bill_stated_rate = (excess_credit_rate_from_bill(bill.raw_json)
                        if bill.raw_json else None)
    if bill.solar_credit_usd is not None and bill.solar_credit_usd > 0:
        rate, source = round(float(bill.solar_credit_usd) / excess, 6), "bill_cash"
    elif bill_stated_rate is not None:
        rate, source = round(bill_stated_rate, 6), "bill_cash"
    else:
        acct = db.get(UtilityAccount, utility_account_id)
        arr = db.get(Array, acct.array_id) if acct and acct.array_id else None
        provider = acct.provider if acct else None
        ped = bill.period_end.date() if isinstance(bill.period_end, datetime) else bill.period_end
        age = array_age_bucket(arr.first_connect_date if arr else None, ped)
        ref = (_account_credit_rate(db, utility_account_id)
               or (_fleet_credit_rate(db, provider=provider, age_bucket=age)
                   if provider else None)
               or DEFAULT_CREDIT_RATE)
        rate, source = round(ref, 6), "reference"
    credit = round(excess * rate, 2)
    ps = bill.period_start.date() if bill.period_start else None
    pe = bill.period_end.date() if bill.period_end else None
    label = pe.strftime("%Y-%m") if pe else None
    return excess, credit, rate, ps, pe, label, source
