"""api/prospectus.py — the Array Prospectus builder (Array Secondary Market v0).

Array Operator already holds the audited version of a solar array's operating
history: third-party-captured utility bills with as-applied credit rates, daily
generation with per-row provenance, a weather-adjusted expectation the seller
doesn't control, the inverter fault/repair record, and the offtaker roster with
invoiced revenue. This module assembles that into ONE verified per-array data
package — the thing a lender (Bruce's refinance) or a buyer (one tenant selling
one array) needs, valuable at n=1 today, with zero new data capture.

Honest by construction (this is the legal line — hold it):
  • It is CAPTURED, TIMESTAMPED and RE-DERIVABLE — NOT immutable, NOT an
    appraisal, NOT an opinion of value. Every section prints its PROVENANCE
    (captured vs owner-reported), its COVERAGE WINDOW (first/last day + counts),
    and never launders an owner-typed CSV into the "captured" tier.
  • Invoiced ≠ collected. There are no payment rails yet — revenue is labeled
    "invoiced," never "collected."
  • A SHA-256 over the data sections makes the ARTIFACT tamper-evident (the same
    underlying data reproduces the same hash). That is the ONLY claim it makes.

No money code. No fees. No brokerage. This is a document surface only. v1
(tenant-to-tenant transfer) and v2 (listings/valuation band) are marked where
they'd go and STOP here.

Pure reads — `build_prospectus(db, tenant, array_id, ...) -> dict`. The dict is
persisted verbatim as an AgentDocument (doc_type='prospectus'); PDF + HTML are
re-rendered from it on demand, so the stored JSON is the single source of truth.
"""
from __future__ import annotations

import hashlib
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import forecasting, rate_schedule
from .generation_sources import ESTIMATE_SOURCES, MEASURED_SOURCES, is_measured
from .models import (
    AlertEvent,
    Array,
    Bill,
    BillingReportSubscription,
    DailyGeneration,
    GmpDailyGeneration,
    Inverter,
    RepairTicket,
    ReportDraft,
    Tenant,
    UtilityAccount,
    WarrantyClaim,
    local_today,
    now,
)

# The disclaimer stamped on every artifact — verbatim, never softened.
DISCLAIMER = (
    "Not an appraisal or an offer. This is a data room only — a captured, "
    "timestamped, re-derivable package built from this array's own operating "
    "history. It is not immutable and not investment advice. Revenue figures are "
    "INVOICED amounts, not collected payments (no payment record exists yet). "
    "Coverage windows and row counts are printed on every section: most telemetry "
    "is young (months, not years); utility bills are the deeper series. Verify "
    "independently before transacting. Array Operator is a software vendor charging "
    "flat SaaS fees for tools — it never negotiates, never takes a success or "
    "brokerage fee, and never touches funds."
)

# Owner-TYPED daily rows. Kept strictly OUT of the "captured" tier so an operator
# CSV can never inflate the verified headline (the honesty redline).
_OWNER_SOURCES = frozenset({"csv", "manual"})

# Bound the single Open-Meteo archive request for the weather-adjusted window.
_EXPECTATION_WINDOW_CAP = 120


def _tier(source: Optional[str]) -> str:
    """Provenance tier for a DailyGeneration.source: captured | owner | estimate."""
    s = (source or "").strip().lower()
    if s in ESTIMATE_SOURCES:
        return "estimate"
    if s in _OWNER_SOURCES:
        return "owner"
    if s in MEASURED_SOURCES:
        return "captured"
    return "other"


def _is_captured(source: Optional[str]) -> bool:
    """A real metered reading that is NOT owner-typed — the verified tier."""
    s = (source or "").strip().lower()
    return is_measured(s) and s not in _OWNER_SOURCES


def _coverage(days: list[date]) -> dict:
    if not days:
        return {"first": None, "last": None, "day_count": 0}
    uniq = sorted(set(days))
    return {"first": uniq[0].isoformat(), "last": uniq[-1].isoformat(),
            "day_count": len(uniq)}


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ───────────────────────────── section builders ──────────────────────────────

def _asset_section(arr: Array, inverters: list[Inverter]) -> dict:
    nameplate = round(sum(float(i.nameplate_kw or 0.0) for i in inverters), 2)
    nameplate_available = any(i.nameplate_kw for i in inverters)
    equipment = [
        {
            "name": i.name,
            "vendor": i.vendor,
            "model": i.model,
            "serial": i.serial,
            "nameplate_kw": (round(float(i.nameplate_kw), 2)
                             if i.nameplate_kw is not None else None),
        }
        for i in inverters
    ]
    return {
        "name": arr.name,
        "portfolio_name": arr.portfolio_name,
        "region": arr.region,
        "fuel_type": arr.fuel_type,
        "cert_registry": arr.cert_registry,
        "nepool_gis_id": arr.nepool_gis_id,
        "first_connect_date": (arr.first_connect_date.date().isoformat()
                               if arr.first_connect_date else None),
        "excluded": bool(arr.excluded),
        "nameplate_kw": nameplate,
        # A utility-only array (no vendor feed) has NO summed kW figure — say so
        # rather than invent one.
        "nameplate_available": bool(nameplate_available),
        "inverter_count": len(inverters),
        "equipment": equipment,
        "location": {
            "latitude": arr.latitude,
            "longitude": arr.longitude,
            "geocode_source": arr.geocode_source,
            "geocoded_address": arr.geocoded_address,
            "tilt_deg": arr.tilt_deg,
            "azimuth_deg": arr.azimuth_deg,
            "geometry_source": arr.geometry_source or ("manual" if arr.tilt_deg is not None else "default"),
        },
        "provenance": "equipment + geometry are captured on connect / operator-set",
    }


def _production_section(db: Session, arr: Array) -> dict:
    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh, DailyGeneration.source)
        .where(DailyGeneration.array_id == arr.id)
        .order_by(DailyGeneration.day)
    ).all()

    monthly: dict[str, dict] = defaultdict(
        lambda: {"captured_kwh": 0.0, "owner_kwh": 0.0, "estimate_kwh": 0.0})
    days_by_tier: dict[str, list[date]] = defaultdict(list)
    rowcount_by_tier: dict[str, int] = defaultdict(int)
    for d, kwh, src in rows:
        tier = _tier(src)
        k = float(kwh or 0.0)
        m = monthly[_month(d)]
        if tier == "captured":
            m["captured_kwh"] += k
        elif tier == "owner":
            m["owner_kwh"] += k
        elif tier == "estimate":
            m["estimate_kwh"] += k
        days_by_tier[tier].append(d)
        rowcount_by_tier[tier] += 1

    monthly_list = [
        {"month": m,
         "captured_kwh": round(v["captured_kwh"], 1),
         "owner_kwh": round(v["owner_kwh"], 1),
         "estimate_kwh": round(v["estimate_kwh"], 1)}
        for m, v in sorted(monthly.items())
    ]

    # Meter-settled overlay (GMP 15-min interval → per-day), where present.
    gmp_rows = db.execute(
        select(GmpDailyGeneration.day, GmpDailyGeneration.kwh)
        .where(GmpDailyGeneration.array_id == arr.id)
        .order_by(GmpDailyGeneration.day)
    ).all()
    gmp_monthly: dict[str, float] = defaultdict(float)
    gmp_days: list[date] = []
    for d, kwh in gmp_rows:
        gmp_monthly[_month(d)] += float(kwh or 0.0)
        gmp_days.append(d)

    return {
        "monthly": monthly_list,
        "coverage": {
            "captured": _coverage(days_by_tier.get("captured", [])),
            "owner_reported": _coverage(days_by_tier.get("owner", [])),
            "estimated_bill_prorate": _coverage(days_by_tier.get("estimate", [])),
        },
        "row_counts": {
            "captured": rowcount_by_tier.get("captured", 0),
            "owner_reported": rowcount_by_tier.get("owner", 0),
            "estimated": rowcount_by_tier.get("estimate", 0),
        },
        "meter_settled": {
            "source": "gmp_api (utility meter, 15-min intervals summed per day)",
            "coverage": _coverage(gmp_days),
            "monthly_kwh": [
                {"month": m, "kwh": round(v, 1)}
                for m, v in sorted(gmp_monthly.items())
            ],
        },
        "provenance_note": (
            "'captured' = metered readings (vendor telemetry, extension captures, "
            "genuine utility reads). 'owner_reported' = operator-typed CSV/manual. "
            "'estimated' = a monthly bill smeared flat across its days "
            "(bill_prorate) — never mixed into the captured total."
        ),
    }


def _expectation_section(db: Session, arr: Array, inverters: list[Inverter]) -> dict:
    """Actual vs weather-adjusted expected over the captured window.

    NOTE (redline): actuals here are CAPTURED-only (vendor/extension/utility reads,
    excluding owner-typed csv/manual) so the headline "produced X% of expectation"
    can never be inflated by operator-entered data. This deliberately diverges from
    the fleet-forecast path, which counts csv/manual as measured. Expectation is
    the independent Open-Meteo plane-of-array model (api/forecasting.py).
    """
    nameplate = sum(float(i.nameplate_kw or 0.0) for i in inverters)
    if nameplate <= 0:
        return {"available": False, "reason": "no_nameplate",
                "note": "No inverter nameplate on file (utility-only array); "
                        "the weather-adjusted expectation needs an installed-kW figure."}

    cap_rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh, DailyGeneration.source)
        .where(DailyGeneration.array_id == arr.id)
        .order_by(DailyGeneration.day)
    ).all()
    captured_by_day: dict[str, float] = defaultdict(float)
    cap_dates: list[date] = []
    for d, kwh, src in cap_rows:
        if _is_captured(src):
            captured_by_day[d.isoformat()] += float(kwh or 0.0)
            cap_dates.append(d)
    if not cap_dates:
        return {"available": False, "reason": "no_captured_production",
                "note": "No captured (metered) daily production on file yet — the "
                        "expectation model won't run on owner-typed rows alone."}

    first, last = min(cap_dates), max(cap_dates)
    span = (last - first).days + 1
    window = max(14, min(_EXPECTATION_WINDOW_CAP, span))
    # Anchor the trailing window to the DATA's last captured day so a stale array
    # is still compared over the days it actually produced (partial-period honesty).
    anchor_today = min(local_today(), last + timedelta(days=1))

    lat, lng = arr.latitude, arr.longitude
    ratio_mode = arr.expected_kwh_per_kw_day is not None and arr.expected_kwh_per_kw_day > 0
    tilt = arr.tilt_deg if arr.tilt_deg is not None else (
        forecasting.default_tilt_deg(lat) if lat is not None else 30.0)
    azimuth = arr.azimuth_deg if arr.azimuth_deg is not None else forecasting.DEFAULT_AZIMUTH_DEG
    pr = arr.performance_ratio if arr.performance_ratio is not None else forecasting.DEFAULT_PR

    try:
        fc = forecasting.build_forecast(
            nameplate_kw=nameplate, lat=lat, lng=lng,
            tilt_deg=tilt, azimuth_deg=azimuth,
            tilt_assumed=arr.tilt_deg is None, azimuth_assumed=arr.azimuth_deg is None,
            geocode_source=arr.geocode_source, geocoded_address=arr.geocoded_address,
            actual_by_day=dict(captured_by_day), window_days=window,
            pr=pr, pr_assumed=arr.performance_ratio is None, today=anchor_today,
            expected_kwh_per_kw_day=arr.expected_kwh_per_kw_day,
        )
    except Exception as e:  # never let a weather-feed hiccup break the artifact
        return {"available": False, "reason": "expectation_error",
                "note": f"Weather-adjusted expectation could not be computed: {e}"}

    out = fc.to_dict()
    out["window_anchor"] = anchor_today.isoformat()
    out["captured_window"] = _coverage(cap_dates)
    out["basis"] = "operator_ratio" if ratio_mode else "weather_model"
    out["actuals_note"] = (
        "Actuals are CAPTURED sources only (owner-typed CSV/manual excluded) so "
        "the ratio can't be inflated by operator-entered data.")
    return out


def _health_section(db: Session, arr: Array, inverters: list[Inverter]) -> dict:
    alerts = db.execute(
        select(AlertEvent).where(AlertEvent.array_id == arr.id)
        .order_by(AlertEvent.created_at)
    ).scalars().all()
    claims = db.execute(
        select(WarrantyClaim).where(WarrantyClaim.array_id == arr.id)
        .order_by(WarrantyClaim.created_at)
    ).scalars().all()
    tickets = db.execute(
        select(RepairTicket).where(RepairTicket.array_id == arr.id)
        .order_by(RepairTicket.opened_at)
    ).scalars().all()

    # When did we START watching this array? (earliest metered day / inverter row)
    first_day = db.execute(
        select(DailyGeneration.day).where(DailyGeneration.array_id == arr.id)
        .order_by(DailyGeneration.day).limit(1)
    ).scalar_one_or_none()
    inv_created = [i.created_at for i in inverters if i.created_at]
    monitoring_since = None
    candidates = []
    if first_day:
        candidates.append(datetime(first_day.year, first_day.month, first_day.day))
    candidates.extend(inv_created)
    if candidates:
        monitoring_since = min(candidates).date().isoformat()

    def _sev_counts(items, attr="severity"):
        c: dict[str, int] = defaultdict(int)
        for it in items:
            c[getattr(it, attr, None) or "unknown"] += 1
        return dict(c)

    recovered = round(sum(float(c.recovered_usd or 0.0) for c in claims), 2)
    expected_low = [
        {"name": i.name, "reason": i.expected_low_reason}
        for i in inverters if i.expected_low
    ]

    return {
        "monitoring_since": monitoring_since,
        "alert_events": {
            "total": len(alerts),
            "open": sum(1 for a in alerts if a.status == "open"),
            "by_severity": _sev_counts(alerts),
            "recent": [
                {"title": a.title, "severity": a.severity, "status": a.status,
                 "inverter_ref": a.inverter_ref,
                 "at": a.created_at.isoformat() if a.created_at else None}
                for a in alerts[-10:]
            ],
        },
        "warranty_claims": {
            "total": len(claims),
            "recovered_usd": recovered,
            "rows": [
                {"fail_type": c.fail_type, "stage": c.stage, "inv_name": c.inv_name,
                 "model": c.model, "recovered_usd": round(float(c.recovered_usd or 0.0), 2),
                 "opened": c.created_at.isoformat() if c.created_at else None,
                 "resolved": c.resolved_at.isoformat() if c.resolved_at else None}
                for c in claims
            ],
        },
        "repair_tickets": {
            "total": len(tickets),
            "open": sum(1 for t in tickets if t.status not in
                        ("resolved", "cancelled", "cleared")),
            "rows": [
                {"fail_type": t.fail_type, "status": t.status, "severity": t.severity,
                 "title": t.title,
                 "opened": t.opened_at.isoformat() if t.opened_at else None,
                 "resolved": t.resolved_at.isoformat() if t.resolved_at else None}
                for t in tickets
            ],
        },
        "expected_low_inverters": expected_low,
        "honesty_note": (
            f"Fault observation began around {monitoring_since or 'the first captured day'}. "
            "Absence of recorded faults BEFORE that is absence of observation, not "
            "absence of faults — most telemetry here is months old, not years."
        ),
    }


def _array_account_ids(db: Session, arr: Array) -> list[int]:
    return list(db.execute(
        select(UtilityAccount.id)
        .where(UtilityAccount.array_id == arr.id, UtilityAccount.deleted_at.is_(None))
    ).scalars().all())


def _subs_for_array(db: Session, tenant: Tenant, arr: Array,
                    account_ids: list[int]) -> list[BillingReportSubscription]:
    """Every offtaker subscription bound to THIS array — via array_id, the
    multi-array allocations JSON, or a utility account that belongs to the array."""
    acct_set = set(account_ids)
    subs = db.execute(
        select(BillingReportSubscription)
        .where(BillingReportSubscription.tenant_id == tenant.id,
               BillingReportSubscription.deleted_at.is_(None))
    ).scalars().all()
    out = []
    for s in subs:
        bound = s.array_id == arr.id
        if not bound and s.array_allocations:
            for al in s.array_allocations:
                try:
                    if int(al.get("array_id")) == arr.id:
                        bound = True
                        break
                except (TypeError, ValueError, AttributeError):
                    continue
        if not bound and s.utility_account_id in acct_set:
            bound = True
        if bound:
            out.append(s)
    return out


def _revenue_section(db: Session, tenant: Tenant, arr: Array,
                     account_ids: list[int]) -> dict:
    subs = _subs_for_array(db, tenant, arr, account_ids)
    offtakers = []
    total_last_sent = 0.0
    for s in subs:
        # Trailing invoiced by month from SENT drafts (real send history).
        sent = db.execute(
            select(ReportDraft.period_label, ReportDraft.amount_usd)
            .where(ReportDraft.subscription_id == s.id,
                   ReportDraft.status == "sent")
            .order_by(ReportDraft.period_label)
        ).all()
        trailing = [
            {"period": p, "invoiced_usd": round(float(a or 0.0), 2)}
            for p, a in sent if a is not None
        ]
        last_amt = float(s.last_sent_amount_usd or 0.0)
        total_last_sent += last_amt
        offtakers.append({
            # PII fields — REDACTED at share time by default (see redact_prospectus).
            "customer_name": s.customer_name,
            "client_email": s.client_email,
            # Terms (never redacted — the deal shape is the value).
            "allocation_pct": s.allocation_pct,
            "array_share_pct": s.array_share_pct,
            "discount_pct": s.discount_pct,
            "net_rate_per_kwh": s.net_rate_per_kwh,
            "rate_per_kwh": s.rate_per_kwh,
            "budget_amount_usd": s.budget_amount_usd,
            "cadence": s.cadence,
            "delivery_mode": s.delivery_mode,
            "last_sent_amount_usd": round(last_amt, 2) if s.last_sent_amount_usd else None,
            "last_sent_period_end": s.last_sent_period_end,
            "trailing_invoiced": trailing,
        })
    return {
        "offtaker_count": len(offtakers),
        "offtakers": offtakers,
        "last_cycle_invoiced_usd": round(total_last_sent, 2),
        "pii_redacted": False,
        "invoiced_not_collected": (
            "All figures are INVOICED amounts. Array Operator knows what was billed, "
            "not what was collected — there are no offtaker payment rails yet."
        ),
    }


def _utility_section(db: Session, arr: Array, account_ids: list[int]) -> dict:
    if not account_ids:
        return {"available": False, "reason": "no_utility_accounts",
                "note": "No linked utility account — the bill series is empty for "
                        "this array (common for inverter-only arrays)."}
    bills = db.execute(
        select(Bill).where(Bill.account_id.in_(account_ids))
        .order_by(Bill.period_end)
    ).scalars().all()
    if not bills:
        return {"available": False, "reason": "no_bills",
                "note": "Linked utility account(s) exist but no bills are captured yet."}

    period_ends = [b.period_end for b in bills if b.period_end]
    pdf_count = sum(1 for b in bills if b.pdf_bytes)

    # Credit-rate history from captured bill line items — the deep, hard-to-fake
    # series. excess_credit_rate_from_bill reads the CREDITED line(s) only; a
    # banked month (excess rolled forward at ~$0) returns None.
    rate_series = []
    banked = 0
    for b in bills:
        if not b.raw_json or not b.period_end:
            continue
        try:
            rate = rate_schedule.excess_credit_rate_from_bill(b.raw_json)
        except Exception:
            rate = None
        is_banked = rate is None
        if is_banked:
            banked += 1
        rate_series.append({
            "period_end": b.period_end.date().isoformat(),
            "credit_rate_per_kwh": (round(rate, 4) if rate is not None else None),
            "banked": is_banked,
            "kwh_sent_to_grid": b.kwh_sent_to_grid,
        })
    rate_series = rate_series[-36:]  # cap size; keep most recent 3 years of bills

    return {
        "bill_count": len(bills),
        "coverage": {
            "first_period_end": (min(period_ends).date().isoformat()
                                 if period_ends else None),
            "last_period_end": (max(period_ends).date().isoformat()
                                if period_ends else None),
        },
        "captured_pdf_count": pdf_count,
        "banked_month_count": banked,
        "credit_rate_history": rate_series,
        "provenance": "bills are the utility portal's own records (raw JSON + PDF "
                      "retained); credit rates are read from real line items, not typed.",
    }


def _estimate_section(expectation: dict, health: dict) -> dict:
    """A reliability SCORE with its inputs printed — NOT a valuation.

    Deliberately stops short of a DCF value band / "opinion of value" (that is
    appraisal territory and a v1/v2 gate — see the plan §5). This is honest math
    on the array's own data, with the disclaimer, and nothing more.
    """
    ratio_pct = expectation.get("ratio_pct") if expectation.get("available") else None
    measured_days = (expectation.get("inputs") or {}).get("measured_days")
    window_days = (expectation.get("inputs") or {}).get("window_days")
    coverage_share = None
    if measured_days is not None and window_days:
        coverage_share = min(1.0, measured_days / window_days)

    score = None
    if ratio_pct is not None and coverage_share is not None:
        r = min(ratio_pct / 100.0, 1.10) / 1.10
        # Weights shown, not a black box; fault term deferred to v1 (a robust
        # fault-day share needs per-day down-state reconstruction).
        score = round(100 * (0.7 * r + 0.3 * coverage_share))

    return {
        "reliability_score": score,
        "inputs": {
            "actual_vs_expected_pct": ratio_pct,
            "data_coverage_share": (round(coverage_share, 2)
                                    if coverage_share is not None else None),
            "weights": {"performance": 0.7, "coverage": 0.3},
            "measured_days": measured_days,
            "window_days": window_days,
        },
        "not_a_valuation": (
            "This is a reliability indicator, NOT an appraisal or an opinion of "
            "value. A defensible valuation additionally needs: a full-year "
            "weather-normalized revenue (not a naive ×12 — solar seasonality "
            "lies), a stated remaining-term and O&M reserve, and collected (not "
            "just invoiced) revenue. Those are a later phase, gated on counsel."
        ),
    }


# ─────────────────────────────── top-level ───────────────────────────────────

def build_prospectus(db: Session, tenant: Tenant, array_id: int, *,
                     window_days: Optional[int] = None,
                     purpose: str = "sale") -> dict:
    """Assemble the full verified prospectus for ONE array. Pure reads.

    `purpose` is echoed into the artifact (sale | refinance). `window_days` is
    reserved for a future explicit-window override; the expectation section
    currently anchors to the captured data window automatically.
    """
    arr = db.execute(
        select(Array).where(Array.id == array_id,
                            Array.tenant_id == tenant.id,
                            Array.deleted_at.is_(None))
    ).scalar_one_or_none()
    if arr is None:
        raise ValueError("array_not_found")

    inverters = db.execute(
        select(Inverter).where(Inverter.array_id == arr.id,
                               Inverter.deleted_at.is_(None))
        .order_by(Inverter.position, Inverter.id)
    ).scalars().all()
    account_ids = _array_account_ids(db, arr)

    asset = _asset_section(arr, inverters)
    production = _production_section(db, arr)
    expectation = _expectation_section(db, arr, inverters)
    health = _health_section(db, arr, inverters)
    revenue = _revenue_section(db, tenant, arr, account_ids)
    utility = _utility_section(db, arr, account_ids)
    estimate = _estimate_section(expectation, health)

    payload = {
        "schema_version": 1,
        "kind": "array_prospectus",
        "purpose": (purpose if purpose in ("sale", "refinance") else "sale"),
        "array_id": arr.id,
        "array_name": arr.name,
        "operator": {
            "company_name": tenant.company_name or tenant.name,
            "operator_name": tenant.operator_name,
        },
        "sections": {
            "asset": asset,
            "production": production,
            "expectation": expectation,
            "health": health,
            "revenue": revenue,
            "utility": utility,
            "estimate": estimate,
        },
        "disclaimer": DISCLAIMER,
        # Volatile — excluded from the content hash so the same data re-derives
        # the same hash (verification: "SHA-256 stable").
        "generated_at": now().isoformat(),
    }
    payload["content_sha256"] = content_sha256(payload)
    return payload


# ───────────────────────────── hash + redaction ──────────────────────────────

def _hashable(payload: dict) -> dict:
    """The payload minus volatile / self-referential keys, for a stable hash."""
    return {k: v for k, v in payload.items()
            if k not in ("generated_at", "content_sha256")}


def content_sha256(payload: dict) -> str:
    canonical = json.dumps(_hashable(payload), sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redact_prospectus(payload: dict) -> dict:
    """Return a copy with offtaker PII stripped (names → 'Offtaker N', emails
    removed). Terms/allocations are preserved — the deal shape IS the value; only
    the identities are hidden. The DEFAULT for any external share."""
    import copy
    out = copy.deepcopy(payload)
    rev = out.get("sections", {}).get("revenue", {})
    for i, o in enumerate(rev.get("offtakers", []), start=1):
        o["customer_name"] = f"Offtaker {i}"
        o["client_email"] = None
    rev["pii_redacted"] = True
    return out


# ─────────────────────────────── renderers ───────────────────────────────────

def _fmt_kwh(x) -> str:
    try:
        return f"{float(x):,.0f} kWh"
    except (TypeError, ValueError):
        return "—"


def _fmt_rate(x) -> str:
    try:
        return f"${float(x):.4f}/kWh"
    except (TypeError, ValueError):
        return "—"


def render_prospectus_html(payload: dict, *, public: bool = False) -> str:
    """A clean, self-contained HTML page — owner preview AND the public share view.
    Sky/octarine flavored, inline CSS (CSP-safe, no external assets)."""
    from xml.sax.saxutils import escape as e

    s = payload.get("sections", {})
    asset = s.get("asset", {})
    prod = s.get("production", {})
    exp = s.get("expectation", {})
    health = s.get("health", {})
    rev = s.get("revenue", {})
    util = s.get("utility", {})
    est = s.get("estimate", {})
    loc = asset.get("location", {})

    def stamp(cov: dict) -> str:
        if not cov or not cov.get("day_count"):
            return '<span class="cov none">no coverage</span>'
        return (f'<span class="cov">{e(str(cov.get("first")))} → '
                f'{e(str(cov.get("last")))} · {cov.get("day_count")} days</span>')

    # Asset
    equip_rows = "".join(
        f"<tr><td>{e(str(i.get('name') or '—'))}</td><td>{e(str(i.get('vendor') or '—'))}</td>"
        f"<td>{e(str(i.get('model') or '—'))}</td><td>{e(str(i.get('serial') or '—'))}</td>"
        f"<td>{i.get('nameplate_kw') if i.get('nameplate_kw') is not None else '—'} kW</td></tr>"
        for i in asset.get("equipment", [])
    ) or '<tr><td colspan="5" class="muted">No inverter equipment on file (utility-only array).</td></tr>'

    # Production monthly
    prod_rows = "".join(
        f"<tr><td>{e(m['month'])}</td><td>{_fmt_kwh(m['captured_kwh'])}</td>"
        f"<td>{_fmt_kwh(m['owner_kwh'])}</td><td>{_fmt_kwh(m['estimate_kwh'])}</td></tr>"
        for m in prod.get("monthly", [])
    ) or '<tr><td colspan="4" class="muted">No daily production captured yet.</td></tr>'

    # Expectation
    if exp.get("available"):
        ratio = exp.get("ratio_pct")
        exp_html = (
            f'<p class="big">{ratio if ratio is not None else "—"}% '
            f'<span class="muted">of weather-adjusted expectation</span></p>'
            f'<p class="muted">Actual {_fmt_kwh(exp.get("actual_kwh"))} vs expected '
            f'{_fmt_kwh(exp.get("expected_matched_kwh"))} over '
            f'{(exp.get("inputs") or {}).get("measured_days","—")} measured days · '
            f'confidence: {e(str(exp.get("confidence","—")))}. {e(str(exp.get("actuals_note","")))}</p>'
        )
    else:
        exp_html = f'<p class="muted">Weather-adjusted expectation unavailable — {e(str(exp.get("reason","")))}. {e(str(exp.get("note","")))}</p>'

    # Health
    ae = health.get("alert_events", {})
    wc = health.get("warranty_claims", {})
    rt = health.get("repair_tickets", {})
    health_html = (
        f'<ul class="stats">'
        f'<li><b>{ae.get("total",0)}</b> alert events ({ae.get("open",0)} open)</li>'
        f'<li><b>{wc.get("total",0)}</b> warranty claims · '
        f'${wc.get("recovered_usd",0):,.0f} recovered</li>'
        f'<li><b>{rt.get("total",0)}</b> repair tickets ({rt.get("open",0)} open)</li>'
        f'</ul><p class="muted">{e(str(health.get("honesty_note","")))}</p>'
    )

    # Revenue
    rev_rows = "".join(
        f"<tr><td>{e(str(o.get('customer_name') or '—'))}</td>"
        f"<td>{(round(o['allocation_pct']*100,1) if o.get('allocation_pct') is not None else '—')}%</td>"
        f"<td>{(str(round(o['discount_pct']*100,1))+'%' if o.get('discount_pct') is not None else '—')}</td>"
        f"<td>{e(str(o.get('cadence') or '—'))}</td>"
        f"<td>{('$'+format(o['last_sent_amount_usd'], ',.2f') if o.get('last_sent_amount_usd') else '—')}</td></tr>"
        for o in rev.get("offtakers", [])
    ) or '<tr><td colspan="5" class="muted">No offtaker subscriptions bound to this array.</td></tr>'
    redact_note = ('<p class="muted">Offtaker identities are redacted in this shared '
                   'view.</p>' if rev.get("pii_redacted") else "")

    # Utility credit-rate history
    if util.get("available") is False:
        util_html = f'<p class="muted">{e(str(util.get("note","")))}</p>'
    else:
        util_html = (
            f'<ul class="stats"><li><b>{util.get("bill_count",0)}</b> bills captured</li>'
            f'<li>{e(str((util.get("coverage") or {}).get("first_period_end") or "—"))} → '
            f'{e(str((util.get("coverage") or {}).get("last_period_end") or "—"))}</li>'
            f'<li><b>{util.get("banked_month_count",0)}</b> banked months</li>'
            f'<li><b>{util.get("captured_pdf_count",0)}</b> bill PDFs retained</li></ul>'
        )
        cr = util.get("credit_rate_history", [])[-12:]
        if cr:
            util_html += '<table><thead><tr><th>Period end</th><th>Credit rate</th><th>Banked</th></tr></thead><tbody>'
            util_html += "".join(
                f"<tr><td>{e(str(r.get('period_end')))}</td>"
                f"<td>{_fmt_rate(r.get('credit_rate_per_kwh')) if not r.get('banked') else '—'}</td>"
                f"<td>{'yes' if r.get('banked') else 'no'}</td></tr>"
                for r in cr
            )
            util_html += "</tbody></table>"

    # Estimate
    score = est.get("reliability_score")
    est_html = (
        (f'<p class="big oct">{score}<span class="muted"> / 100 reliability</span></p>'
         if score is not None else '<p class="muted">Reliability score unavailable (needs captured production + expectation).</p>')
        + f'<p class="muted">{e(str(est.get("not_a_valuation","")))}</p>'
    )

    nameplate = (f'{asset.get("nameplate_kw")} kW'
                 if asset.get("nameplate_available") else "not available (utility-only array)")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Array Prospectus — {e(str(payload.get('array_name') or 'Array'))}</title>
<style>
:root{{--ink:#0e1420;--muted:#5a6572;--line:#e2e8f0;--card:#fff;--oct:#7c3aed;
--sky1:#1E90E8;--sky2:#56B4F0;--sky3:#BEE3FA;}}
*{{box-sizing:border-box}}
body{{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
color:var(--ink);background:linear-gradient(180deg,var(--sky1),var(--sky2) 42%,var(--sky3));
min-height:100vh;padding:24px 16px}}
.wrap{{max-width:860px;margin:0 auto}}
.sheet{{background:rgba(255,255,255,.86);backdrop-filter:blur(18px) saturate(1.5);
border:1px solid rgba(255,255,255,.6);border-radius:22px;
box-shadow:0 12px 40px rgba(20,60,120,.16);padding:28px 30px;margin-bottom:18px}}
h1{{font-size:30px;letter-spacing:-.02em;margin:0 0 4px}}
h2{{font-size:14px;text-transform:uppercase;letter-spacing:.10em;color:var(--muted);
margin:0 0 12px;font-weight:800}}
.eyebrow{{text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:800;
color:var(--oct);margin:0 0 6px}}
.big{{font-size:34px;font-weight:800;letter-spacing:-.02em;margin:6px 0}}
.oct{{color:var(--oct)}}
.muted{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:8px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
ul.stats{{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:10px}}
ul.stats li{{background:#f3f6fa;border-radius:10px;padding:8px 12px;font-size:13.5px}}
.cov{{display:inline-block;background:#eef2f7;color:var(--muted);border-radius:999px;
padding:2px 10px;font-size:11.5px;margin-left:8px}}
.cov.none{{background:#fef3c7;color:#92400e}}
.meta{{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:13px;color:var(--muted)}}
.disc{{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px;margin-top:6px}}
.hash{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;word-break:break-all;color:var(--muted)}}
</style></head><body><div class="wrap">
<div class="sheet">
<p class="eyebrow">Array Prospectus · {e(str(payload.get('purpose','sale')).title())} · data room</p>
<h1>{e(str(payload.get('array_name') or 'Array'))}</h1>
<div class="meta">
<span>Operator: {e(str((payload.get('operator') or {}).get('company_name') or '—'))}</span>
<span>Nameplate: {e(str(nameplate))}</span>
<span>{e(str(loc.get('geocoded_address') or 'location not geocoded'))}</span>
<span>Generated {e(str(payload.get('generated_at') or '')[:19])}</span>
</div>
</div>

<div class="sheet"><h2>The asset</h2>
<div class="meta" style="margin-bottom:10px">
<span>NEPOOL GIS: {e(str(asset.get('nepool_gis_id') or '—'))}</span>
<span>Fuel: {e(str(asset.get('fuel_type') or 'solar'))}</span>
<span>First connect: {e(str(asset.get('first_connect_date') or 'unknown'))}</span>
<span>Inverters: {asset.get('inverter_count',0)}</span>
</div>
<table><thead><tr><th>Inverter</th><th>Vendor</th><th>Model</th><th>Serial</th><th>Nameplate</th></tr></thead>
<tbody>{equip_rows}</tbody></table></div>

<div class="sheet"><h2>Production record {stamp(prod.get('coverage',{}).get('captured',{}))}</h2>
<table><thead><tr><th>Month</th><th>Captured</th><th>Owner-reported</th><th>Estimated</th></tr></thead>
<tbody>{prod_rows}</tbody></table>
<p class="muted" style="margin-top:10px">{e(str(prod.get('provenance_note','')))}</p></div>

<div class="sheet"><h2>Weather-adjusted performance</h2>{exp_html}</div>

<div class="sheet"><h2>Equipment health</h2>{health_html}</div>

<div class="sheet"><h2>Revenue (invoiced)</h2>
<table><thead><tr><th>Offtaker</th><th>Allocation</th><th>Discount</th><th>Cadence</th><th>Last invoiced</th></tr></thead>
<tbody>{rev_rows}</tbody></table>
{redact_note}
<p class="muted">{e(str(rev.get('invoiced_not_collected','')))}</p></div>

<div class="sheet"><h2>The utility record</h2>{util_html}
<p class="muted" style="margin-top:8px">{e(str(util.get('provenance','')))}</p></div>

<div class="sheet"><h2>Reliability indicator</h2>{est_html}</div>

<div class="sheet">
<p class="disc">{e(str(payload.get('disclaimer','')))}</p>
<p class="hash">Artifact SHA-256: {e(str(payload.get('content_sha256') or '—'))}</p>
</div>
</div></body></html>"""


def render_prospectus_pdf(payload: dict) -> bytes:
    """Re-render the stored prospectus JSON to a one-file PDF (reportlab platypus).
    Mirrors the billing/invoice.py idiom; no external service, no matplotlib."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from xml.sax.saxutils import escape as e

    s = payload.get("sections", {})
    asset = s.get("asset", {})
    prod = s.get("production", {})
    exp = s.get("expectation", {})
    health = s.get("health", {})
    rev = s.get("revenue", {})
    util = s.get("utility", {})
    est = s.get("estimate", {})
    loc = asset.get("location", {})

    styles = getSampleStyleSheet()
    oct_col = colors.HexColor("#7c3aed")
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=20, spaceAfter=2,
                        textColor=colors.HexColor("#0e1420"))
    eyebrow = ParagraphStyle("eb", parent=styles["Normal"], fontSize=8,
                             textColor=oct_col, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11,
                        textColor=colors.HexColor("#334155"), spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13)
    muted = ParagraphStyle("muted", parent=styles["Normal"], fontSize=8,
                           textColor=colors.HexColor("#5a6572"), leading=11)

    story = []
    story.append(Paragraph(f"Array Prospectus · {e(str(payload.get('purpose','sale')).title())} · data room", eyebrow))
    story.append(Paragraph(e(str(payload.get("array_name") or "Array")), h1))
    nameplate = (f"{asset.get('nameplate_kw')} kW" if asset.get("nameplate_available")
                 else "not available (utility-only array)")
    story.append(Paragraph(
        f"Operator: {e(str((payload.get('operator') or {}).get('company_name') or '—'))} · "
        f"Nameplate: {e(str(nameplate))} · "
        f"{e(str(loc.get('geocoded_address') or 'location not geocoded'))} · "
        f"Generated {e(str(payload.get('generated_at') or '')[:19])}", muted))

    def _tbl(headers, rows, widths=None):
        data = [headers] + rows
        t = Table(data, colWidths=widths, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f6fa")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#5a6572")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    # Asset
    story.append(Paragraph("The asset", h2))
    story.append(Paragraph(
        f"NEPOOL GIS: {e(str(asset.get('nepool_gis_id') or '—'))} · "
        f"Fuel: {e(str(asset.get('fuel_type') or 'solar'))} · "
        f"First connect: {e(str(asset.get('first_connect_date') or 'unknown'))} · "
        f"Inverters: {asset.get('inverter_count',0)}", body))
    equip = asset.get("equipment", [])
    if equip:
        story.append(Spacer(1, 4))
        story.append(_tbl(
            ["Inverter", "Vendor", "Model", "Serial", "kW"],
            [[str(i.get("name") or "—"), str(i.get("vendor") or "—"),
              str(i.get("model") or "—"), str(i.get("serial") or "—"),
              str(i.get("nameplate_kw") if i.get("nameplate_kw") is not None else "—")]
             for i in equip],
            widths=[1.4 * inch, 0.8 * inch, 1.3 * inch, 1.6 * inch, 0.6 * inch]))

    # Production
    cov = (prod.get("coverage", {}).get("captured", {}) or {})
    story.append(Paragraph("Production record", h2))
    story.append(Paragraph(
        f"Captured coverage: {e(str(cov.get('first') or '—'))} → {e(str(cov.get('last') or '—'))} "
        f"· {cov.get('day_count',0)} days", muted))
    monthly = prod.get("monthly", [])
    if monthly:
        story.append(Spacer(1, 4))
        story.append(_tbl(
            ["Month", "Captured kWh", "Owner kWh", "Estimated kWh"],
            [[m["month"], f"{m['captured_kwh']:,.0f}", f"{m['owner_kwh']:,.0f}",
              f"{m['estimate_kwh']:,.0f}"] for m in monthly[-24:]]))

    # Expectation
    story.append(Paragraph("Weather-adjusted performance", h2))
    if exp.get("available"):
        story.append(Paragraph(
            f"<b>{exp.get('ratio_pct','—')}%</b> of weather-adjusted expectation — "
            f"actual {exp.get('actual_kwh','—')} kWh vs expected "
            f"{exp.get('expected_matched_kwh','—')} kWh over "
            f"{(exp.get('inputs') or {}).get('measured_days','—')} measured days "
            f"(confidence: {e(str(exp.get('confidence','—')))}).", body))
        story.append(Paragraph(e(str(exp.get("actuals_note", ""))), muted))
    else:
        story.append(Paragraph(
            f"Unavailable — {e(str(exp.get('reason','')))}. {e(str(exp.get('note','')))}", muted))

    # Health
    ae, wc, rt = health.get("alert_events", {}), health.get("warranty_claims", {}), health.get("repair_tickets", {})
    story.append(Paragraph("Equipment health", h2))
    story.append(Paragraph(
        f"{ae.get('total',0)} alert events ({ae.get('open',0)} open) · "
        f"{wc.get('total',0)} warranty claims (${wc.get('recovered_usd',0):,.0f} recovered) · "
        f"{rt.get('total',0)} repair tickets ({rt.get('open',0)} open).", body))
    story.append(Paragraph(e(str(health.get("honesty_note", ""))), muted))

    # Revenue
    story.append(Paragraph("Revenue (invoiced)", h2))
    offts = rev.get("offtakers", [])
    if offts:
        story.append(_tbl(
            ["Offtaker", "Allocation", "Discount", "Cadence", "Last invoiced"],
            [[str(o.get("customer_name") or "—"),
              (f"{o['allocation_pct']*100:.1f}%" if o.get("allocation_pct") is not None else "—"),
              (f"{o['discount_pct']*100:.1f}%" if o.get("discount_pct") is not None else "—"),
              str(o.get("cadence") or "—"),
              (f"${o['last_sent_amount_usd']:,.2f}" if o.get("last_sent_amount_usd") else "—")]
             for o in offts]))
    else:
        story.append(Paragraph("No offtaker subscriptions bound to this array.", muted))
    if rev.get("pii_redacted"):
        story.append(Paragraph("Offtaker identities are redacted in this shared view.", muted))
    story.append(Paragraph(e(str(rev.get("invoiced_not_collected", ""))), muted))

    # Utility
    story.append(Paragraph("The utility record", h2))
    if util.get("available") is False:
        story.append(Paragraph(e(str(util.get("note", ""))), muted))
    else:
        ucov = util.get("coverage", {})
        story.append(Paragraph(
            f"{util.get('bill_count',0)} bills · {e(str(ucov.get('first_period_end') or '—'))} → "
            f"{e(str(ucov.get('last_period_end') or '—'))} · "
            f"{util.get('banked_month_count',0)} banked months · "
            f"{util.get('captured_pdf_count',0)} bill PDFs retained.", body))

    # Estimate
    story.append(Paragraph("Reliability indicator", h2))
    sc = est.get("reliability_score")
    story.append(Paragraph(
        (f"<b>{sc} / 100</b> reliability." if sc is not None
         else "Reliability score unavailable (needs captured production + expectation)."), body))
    story.append(Paragraph(e(str(est.get("not_a_valuation", ""))), muted))

    # Disclaimer + hash
    story.append(Spacer(1, 12))
    story.append(Paragraph(e(str(payload.get("disclaimer", ""))), muted))
    story.append(Paragraph(f"Artifact SHA-256: {e(str(payload.get('content_sha256') or '—'))}", muted))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch,
                            bottomMargin=0.6 * inch, leftMargin=0.7 * inch,
                            rightMargin=0.7 * inch,
                            title=f"Array Prospectus — {payload.get('array_name') or 'Array'}")
    doc.build(story)
    return buf.getvalue()
