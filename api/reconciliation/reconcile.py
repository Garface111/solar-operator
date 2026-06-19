"""
Production-vs-settlement reconciliation engine.

reconcile_array() computes, for one array over a window, whether metered inverter
production reconciles to utility-settled generation, and emits a ReconResult with
an explicit status and the validation gates that produced it.

Read-only with respect to the DB. Spec: vt-solar-intel/host-meter-boundary-fix-spec.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Bill, DailyGeneration, Inverter, Array
from .classify import classify_array, Classification

# ── Tunable thresholds (spec §4) ────────────────────────────────────────────
FEED_COMPLETENESS_FLOOR = 0.90   # production days / billing days
LEAK_THRESHOLD_PCT = 10.0        # |variance_pct| above this is a candidate leak
NAMEPLATE_CF_LOW = 0.05          # implausibly low monthly capacity factor
NAMEPLATE_CF_HIGH = 0.30         # implausibly high monthly capacity factor
FULL_COVERAGE_FLOOR = 0.90       # group feed considered "complete" at/above this

# kWh sources that count as real metered production (not bill pro-rate).
PRODUCTION_SOURCES = ("solaredge", "csv", "extension_pull", "extension_pull_corrected",
                      "gmp_portal_scrape", "manual")

# Of those, the ones from a party INDEPENDENT of the utility (the inverter vendor
# or a direct meter export the owner controls). A variance backed only by
# utility-sourced production (GMP interval / GMP portal scrape) is reconciling the
# utility against itself — still useful ("are you credited for metered kWh?") but
# lower-confidence, so leaks from it are flagged as needs-confirm, never asserted
# with the same authority as an independent-feed leak.
INDEPENDENT_SOURCES = ("solaredge", "csv", "manual")

# VT statewide blended residential credit rate, PUC eff. Apr 2024. Per-array
# vintage/category may differ; callers can override via rate arg.
VT_DEFAULT_RATE = 0.18398


@dataclass
class ReconResult:
    array_id: int
    array_name: str
    classification: str
    window_start: date | None
    window_end: date | None
    settlement_kwh: float          # authoritative utility-settled generation
    production_kwh: float          # metered inverter production over the windows
    coverage_ratio: float | None   # production / settlement
    variance_pct: float | None     # 100*(prod-settle)/settle  (single_site only)
    dollars_at_risk: float         # variance translated to $ (single_site leaks only)
    status: str                    # ok | incomplete_monitoring | leak | insufficient_data
    report_leak: bool
    gates: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    n_bills: int = 0


def _bills_for_account(db: Session, account_id: int, ws: date | None, we: date | None) -> list[Bill]:
    q = select(Bill).where(
        Bill.account_id == account_id,
        Bill.parse_status == "parsed",
        Bill.period_start.is_not(None),
        Bill.period_end.is_not(None),
    )
    bills = list(db.execute(q).scalars())
    out = []
    for b in bills:
        pe = b.period_end.date()
        ps = b.period_start.date()
        if ws and we:
            # window overlap on the service period
            if pe < ws or ps > we:
                continue
        out.append(b)
    return out


def _production_over_window(db: Session, array_id: int, ps: date, pe: date) -> tuple[float, int, float]:
    """Sum metered production + distinct production-days over a bill service window.

    Merges two sources per day (no double-count): the DailyGeneration table
    (inverter/CSV/portal) and the GMP daily-generation sponge (read via the
    gmp_daily_read seam). DailyGeneration wins on a day both cover. Returns
    (total_kwh, distinct_days, independent_kwh) where independent_kwh is the
    portion from a party independent of the utility (INDEPENDENT_SOURCES).
    """
    rows = list(db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh, DailyGeneration.source).where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day >= ps,
            DailyGeneration.day <= pe,
            DailyGeneration.source.in_(PRODUCTION_SOURCES),
        )
    ))
    per_day: dict[date, float] = {}
    independent_days: set[date] = set()
    for r in rows:
        per_day[r.day] = per_day.get(r.day, 0.0) + float(r.kwh)
        if r.source in INDEPENDENT_SOURCES:
            independent_days.add(r.day)
    independent_kwh = sum(v for d, v in per_day.items() if d in independent_days)

    # Fill days the DailyGeneration table doesn't cover with the GMP daily sponge
    # (utility meter — counts as production, but NOT independent).
    try:
        from ..reports import gmp_daily_read as _gdr
        for pt in _gdr.get_daily_series(array_id, start=ps, end=pe, db=db):
            d = pt["day"]
            if d not in per_day:
                per_day[d] = float(pt["kwh"])
    except Exception:
        pass  # GMP seam unavailable → fall back to DailyGeneration only

    kwh = sum(per_day.values())
    days = len(per_day)
    return kwh, days, independent_kwh


def _nameplate_kw(db: Session, array_id: int) -> float:
    total = db.execute(
        select(func.coalesce(func.sum(Inverter.nameplate_kw), 0.0)).where(
            Inverter.array_id == array_id,
            Inverter.deleted_at.is_(None),
        )
    ).scalar_one()
    return float(total or 0.0)


def reconcile_array(
    db: Session,
    array_id: int,
    window_start: date | None = None,
    window_end: date | None = None,
    rate: float = VT_DEFAULT_RATE,
) -> ReconResult:
    arr = db.get(Array, array_id)
    array_name = arr.name if arr else str(array_id)
    cls: Classification = classify_array(db, array_id, window_start, window_end)
    notes = list(cls.notes)

    # Pick the account(s) whose bills carry the authoritative settlement total.
    if cls.classification == "group_net_metered":
        host_ids = [cls.host_account_id] if cls.host_account_id else []
    else:
        # single-site: all non-deleted accounts on the array
        from .classify import _array_accounts
        host_ids = [a.id for a in _array_accounts(db, array_id)]

    settlement_kwh = 0.0
    production_kwh = 0.0
    independent_kwh = 0.0
    feed_complete_all = True
    n_bills = 0
    nameplate = _nameplate_kw(db, array_id)
    cf_flags: list[bool] = []

    for acct_id in host_ids:
        for b in _bills_for_account(db, acct_id, window_start, window_end):
            if b.kwh_generated is None:
                continue
            n_bills += 1
            settlement_kwh += float(b.kwh_generated)
            ps, pe = b.period_start.date(), b.period_end.date()
            pk, pdays, ik = _production_over_window(db, array_id, ps, pe)
            production_kwh += pk
            independent_kwh += ik
            billing_days = b.billing_days or ((pe - ps).days + 1)
            if billing_days > 0 and (pdays / billing_days) < FEED_COMPLETENESS_FLOOR:
                feed_complete_all = False
            # nameplate plausibility per window
            if nameplate > 0 and billing_days > 0:
                expected = nameplate * 24 * billing_days
                cf = pk / expected if expected else 0.0
                cf_flags.append(NAMEPLATE_CF_LOW <= cf <= NAMEPLATE_CF_HIGH)

    coverage_ratio = (production_kwh / settlement_kwh) if settlement_kwh > 0 else None
    variance_pct = (
        100.0 * (production_kwh - settlement_kwh) / settlement_kwh
        if settlement_kwh > 0 else None
    )
    # Is the production leg backed by a feed independent of the utility? (Most of
    # it from an inverter vendor / owner export, not GMP's own meter.)
    independent_feed = production_kwh > 0 and (independent_kwh / production_kwh) >= 0.5

    # ── Gates (spec §4) ─────────────────────────────────────────────────────
    gate_feed_complete = feed_complete_all and n_bills > 0
    gate_nameplate_ok = (nameplate <= 0) or (len(cf_flags) == 0) or all(cf_flags)
    gate_group_coverage_ok = (
        cls.classification != "group_net_metered"
        or (coverage_ratio is not None and coverage_ratio >= FULL_COVERAGE_FLOOR)
    )
    gates = {
        "feed_complete": gate_feed_complete,
        "nameplate_ok": gate_nameplate_ok,
        "group_coverage_ok": gate_group_coverage_ok,
    }

    # ── Status decision (spec §3 + §4.4) ────────────────────────────────────
    dollars_at_risk = 0.0
    if n_bills == 0 or settlement_kwh <= 0:
        status = "insufficient_data"
        report_leak = False
        notes.append("No parsed bills with generation in window.")
    elif cls.classification == "group_net_metered" and not gate_group_coverage_ok:
        status = "incomplete_monitoring"
        report_leak = False
        cov = coverage_ratio or 0.0
        notes.append(
            f"SolarEdge monitoring covers {cov:.0%} of host generation; full-boundary "
            "production monitoring requires onboarding the additional SolarEdge "
            "site_id(s) for the unmonitored remainder (see spec §6)."
        )
    elif not gate_feed_complete:
        status = "insufficient_data"
        report_leak = False
        notes.append("Production feed incomplete for one or more bill windows — not a leak.")
    elif not gate_nameplate_ok:
        status = "insufficient_data"
        report_leak = False
        notes.append("Production implies implausible capacity factor — data-hygiene flag, not a leak.")
    else:
        # complete, plausible, full-coverage feed
        variance_is_leak = (
            cls.classification == "single_site"
            and variance_pct is not None
            and abs(variance_pct) > LEAK_THRESHOLD_PCT
        )
        if variance_is_leak and not independent_feed:
            # The variance is real, but production came from the utility's OWN
            # meter (GMP interval) — reconciling the utility against itself. Flag
            # it for the owner to confirm, don't assert it as a definite leak.
            status = "leak_unconfirmed"
            report_leak = False
            notes.append(
                "Metered generation diverges from utility settlement by "
                f"{variance_pct:+.1f}%, but the production figure is the utility's "
                "own meter data — connect the inverter monitoring (SolarEdge/Chint/"
                "Fronius) to confirm this as an independent leak."
            )
            if variance_pct is not None:
                dollars_at_risk = abs(production_kwh - settlement_kwh) * rate
        else:
            report_leak = variance_is_leak
            status = "leak" if report_leak else "ok"
            if report_leak and variance_pct is not None:
                # dollars: the kWh gap valued at the credit rate
                dollars_at_risk = abs(production_kwh - settlement_kwh) * rate

    gates["independent_feed"] = independent_feed

    return ReconResult(
        array_id=array_id,
        array_name=array_name,
        classification=cls.classification,
        window_start=window_start,
        window_end=window_end,
        settlement_kwh=round(settlement_kwh, 1),
        production_kwh=round(production_kwh, 1),
        coverage_ratio=round(coverage_ratio, 4) if coverage_ratio is not None else None,
        variance_pct=round(variance_pct, 2) if variance_pct is not None else None,
        dollars_at_risk=round(dollars_at_risk, 2),
        status=status,
        report_leak=report_leak,
        gates=gates,
        notes=notes,
        n_bills=n_bills,
    )
