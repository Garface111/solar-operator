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
PRODUCTION_SOURCES = ("solaredge", "csv", "extension_pull", "gmp_portal_scrape", "manual")

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


def _production_over_window(db: Session, array_id: int, ps: date, pe: date) -> tuple[float, int]:
    """Sum metered production + distinct production-days over a bill service window."""
    rows = list(db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh).where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day >= ps,
            DailyGeneration.day <= pe,
            DailyGeneration.source.in_(PRODUCTION_SOURCES),
        )
    ))
    kwh = sum(float(r.kwh) for r in rows)
    days = len({r.day for r in rows})
    return kwh, days


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
            pk, pdays = _production_over_window(db, array_id, ps, pe)
            production_kwh += pk
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
        report_leak = (
            cls.classification == "single_site"
            and variance_pct is not None
            and abs(variance_pct) > LEAK_THRESHOLD_PCT
        )
        status = "leak" if report_leak else "ok"
        if report_leak and variance_pct is not None:
            # dollars: the kWh gap valued at the credit rate
            dollars_at_risk = abs(production_kwh - settlement_kwh) * rate

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
