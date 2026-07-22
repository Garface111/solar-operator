"""Orchestrate portfolio / per-array performance verification snapshots.

Builds on forecasting + meter-primary boundary. Pure assembly; network only
via forecasting POA when expected energy is weather-based.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select

from ..db import SessionLocal
from ..generation_sources import UTILITY_REAL_SOURCES, is_measured, ESTIMATE_SOURCES
from ..models import Array, DailyGeneration, Inverter, Tenant

from .availability import compute_availability
from .boundary import (
    BOUNDARY_METER,
    BOUNDARY_UNAVAILABLE,
    classify_series_boundary,
    select_measured_for_day,
)
from .causes import cause_to_dict, infer_cause
from .persistence import (
    DEFAULT_DEVIATION_THRESHOLD,
    classify_deviation,
)
from .standards import IEC_ALIGNMENT_NOTE, METHOD_SUMMARY, REPORT_FOOTER

log = logging.getLogger("perf_verification")


def _threshold_for_tenant(tenant: Tenant) -> float:
    thr = getattr(tenant, "verification_deviation_threshold", None)
    if thr is None:
        return DEFAULT_DEVIATION_THRESHOLD
    try:
        t = float(thr)
        return t if 0.01 <= t <= 0.5 else DEFAULT_DEVIATION_THRESHOLD
    except (TypeError, ValueError):
        return DEFAULT_DEVIATION_THRESHOLD


def _load_day_rows(db, array_id: int, start: date, end: date) -> dict[str, list[dict]]:
    """iso_day -> list of {kwh, source}."""
    rows = db.execute(
        select(DailyGeneration).where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day >= start,
            DailyGeneration.day <= end,
        )
    ).scalars().all()
    by: dict[str, list[dict]] = {}
    for r in rows:
        src = (r.source or "").lower()
        if src in ESTIMATE_SOURCES:
            continue
        if r.kwh is None or r.kwh < 0:
            continue
        if not (src in UTILITY_REAL_SOURCES or is_measured(src)):
            continue
        iso = r.day.isoformat()
        by.setdefault(iso, []).append({"kwh": float(r.kwh), "source": src})
    return by


def _measured_series(
    db, array_id: int, start: date, end: date
) -> tuple[dict[str, float], dict[str, str], list[str]]:
    """Returns actual_by_day, boundary_by_day, all day boundaries list."""
    by = _load_day_rows(db, array_id, start, end)
    actual: dict[str, float] = {}
    boundaries: dict[str, str] = {}
    for iso, rows in by.items():
        pick = select_measured_for_day(rows)
        if pick["kwh"] is not None:
            actual[iso] = float(pick["kwh"])
            boundaries[iso] = pick["boundary"]
    return actual, boundaries, list(boundaries.values())


def _inverter_status_hint(db, arr: Array) -> tuple[Optional[str], bool, Optional[str]]:
    """Return (worst_peer_status, any_expected_low, fault_code_hint)."""
    invs = db.execute(
        select(Inverter).where(
            Inverter.array_id == arr.id,
            Inverter.deleted_at.is_(None),
        )
    ).scalars().all()
    if not invs:
        return None, False, None
    expected_low = any(bool(getattr(i, "expected_low", False)) for i in invs)
    # Prefer explicit last status fields if present
    status = None
    fault = None
    for i in invs:
        st = getattr(i, "last_status", None) or getattr(i, "status", None)
        if st in ("fault", "dead"):
            status = st
            fault = getattr(i, "last_error_code", None) or fault
            break
        if st == "comm_gap" and status is None:
            status = "comm_gap"
        if st == "underperforming" and status is None:
            status = "underperforming"
    return status, expected_low, fault


def build_array_verification(
    db,
    arr: Array,
    *,
    window_days: int = 30,
    today: Optional[date] = None,
    threshold: float = DEFAULT_DEVIATION_THRESHOLD,
    _poa_by_day: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Full verification dict for one array. Never fabricates PI."""
    from .. import forecasting
    from ..array_owners import _array_nameplate_kw, _ensure_array_geocoded

    today = today or datetime.utcnow().date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)

    nameplate = _array_nameplate_kw(db, arr)
    base = {
        "array_id": arr.id,
        "array_name": arr.name,
        "window_days": window_days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "available": False,
        "standards_note": IEC_ALIGNMENT_NOTE,
    }
    if nameplate <= 0:
        return {**base, "reason": "no_nameplate"}

    expected_ratio = arr.expected_kwh_per_kw_day
    has_loc = _ensure_array_geocoded(db, arr)
    if expected_ratio is None and not has_loc:
        return {**base, "reason": "no_location"}

    actual_by_day, boundary_by_day, b_list = _measured_series(db, arr.id, start, end)
    series_boundary = classify_series_boundary(b_list)

    tilt_assumed = arr.tilt_deg is None
    az_assumed = arr.azimuth_deg is None
    tilt = arr.tilt_deg if arr.tilt_deg is not None else (
        forecasting.default_tilt_deg(arr.latitude) if has_loc else 0.0
    )
    az = (
        arr.azimuth_deg
        if arr.azimuth_deg is not None
        else forecasting.DEFAULT_AZIMUTH_DEG
    )
    pr_assumed = arr.performance_ratio is None
    pr = (
        float(arr.performance_ratio)
        if arr.performance_ratio is not None
        else forecasting.DEFAULT_PR
    )

    fc = forecasting.build_forecast(
        nameplate_kw=nameplate,
        lat=arr.latitude,
        lng=arr.longitude,
        tilt_deg=tilt,
        azimuth_deg=az,
        tilt_assumed=tilt_assumed,
        azimuth_assumed=az_assumed,
        geocode_source=arr.geocode_source,
        geocoded_address=arr.geocoded_address,
        actual_by_day=actual_by_day,
        window_days=window_days,
        today=today,
        expected_kwh_per_kw_day=expected_ratio,
        pr=pr,
        pr_assumed=pr_assumed,
        _poa_by_day=_poa_by_day,
    )
    if not fc.available:
        return {
            **base,
            "reason": fc.reason,
            "boundary": series_boundary,
            "measured_days": len(actual_by_day),
        }

    daily = []
    residuals_for_class = []
    sunny_res, cloudy_res = [], []
    for d in fc.days:
        residual = None
        if d.actual_kwh is not None and d.expected_kwh > 0:
            residual = (d.actual_kwh - d.expected_kwh) / d.expected_kwh
            residuals_for_class.append({"day": d.day, "residual": residual})
            if d.sunny:
                sunny_res.append(residual)
            else:
                cloudy_res.append(residual)
        daily.append({
            "day": d.day,
            "actual_kwh": d.actual_kwh,
            "expected_kwh": round(d.expected_kwh, 1),
            "poa_kwh_m2": (
                round(d.poa_kwh_m2, 2) if d.poa_kwh_m2 is not None else None
            ),
            "residual": round(residual, 4) if residual is not None else None,
            "boundary": boundary_by_day.get(d.day, BOUNDARY_UNAVAILABLE),
            "sunny": d.sunny,
            "status": None,
        })

    dev = classify_deviation(
        residuals_for_class,
        threshold=threshold,
    )
    peer_status, expected_low, fault = _inverter_status_hint(db, arr)
    cause = infer_cause(
        deviation_label=dev.label,
        residual_mean=dev.magnitude_mean,
        peer_status=peer_status,
        expected_low=expected_low,
        sunny_day_residuals=sunny_res,
        cloudy_day_residuals=cloudy_res,
        measured_days=fc.inputs.get("measured_days") or len(actual_by_day),
        window_days=window_days,
        boundary=series_boundary,
        fault_code=fault,
    )
    avail = compute_availability(window_days=window_days, daily=daily)

    # Loss estimate vs expected on matched days
    loss_kwh = None
    if fc.expected_matched_kwh > 0 and fc.actual_kwh is not None:
        loss_kwh = round(fc.expected_matched_kwh - fc.actual_kwh, 1)

    return {
        **base,
        "available": True,
        "reason": None,
        "boundary": series_boundary,
        "nameplate_kw": round(nameplate, 2),
        "performance_index": fc.performance_ratio_measured,
        "performance_ratio_model": pr,
        "performance_ratio_assumed": pr_assumed,
        "expected_kwh": round(fc.expected_kwh, 1),
        "expected_matched_kwh": round(fc.expected_matched_kwh, 1),
        "actual_kwh": round(fc.actual_kwh, 1),
        "ratio_pct": (
            round(fc.actual_kwh / fc.expected_matched_kwh * 100)
            if fc.expected_matched_kwh > 0 else None
        ),
        "loss_kwh_vs_expected": loss_kwh,
        "confidence": fc.confidence,
        "measured_days": fc.inputs.get("measured_days"),
        "inputs": fc.inputs,
        "deviation": {
            "label": dev.label,
            "threshold": dev.threshold,
            "magnitude_mean": dev.magnitude_mean,
            "duration_days": dev.duration_days,
            "priority": dev.priority,
            "detail": dev.detail,
        },
        "cause": cause_to_dict(cause),
        "availability": avail.to_dict(),
        "daily": daily,
        "report_footer": REPORT_FOOTER,
    }


def build_portfolio_verification(
    tenant: Tenant,
    *,
    window_days: int = 30,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Portfolio rollup for an Array Operator tenant."""
    from sqlalchemy import exists

    from .. import forecasting
    from ..array_owners import _array_nameplate_kw, _ensure_array_geocoded
    from ..models import Inverter as Inv

    today = today or datetime.utcnow().date()
    thr = _threshold_for_tenant(tenant)
    end = today - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)

    poa_memo: dict[tuple, dict] = {}

    def poa_for(lat, lng, tilt, az):
        key = (round(lat, 3), round(lng, 3), round(tilt, 1), round(az, 1))
        if key not in poa_memo:
            poa_memo[key] = forecasting.fetch_poa_daily(lat, lng, tilt, az, start, end)
        return poa_memo[key]

    arrays_out: list[dict] = []
    skipped: list[dict] = []
    fleet_act = 0.0
    fleet_exp_m = 0.0
    priorities: list[float] = []

    with SessionLocal() as db:
        # Re-bind tenant in session
        t = db.get(Tenant, tenant.id) or tenant
        thr = _threshold_for_tenant(t)
        arrays = db.execute(
            select(Array).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.excluded.is_(False),
                exists().where(
                    Inv.array_id == Array.id,
                    Inv.deleted_at.is_(None),
                ),
            )
        ).scalars().all()

        for arr in arrays:
            nameplate = _array_nameplate_kw(db, arr)
            has_loc = _ensure_array_geocoded(db, arr)
            poa = None
            if (
                arr.expected_kwh_per_kw_day is None
                and has_loc
                and arr.latitude is not None
                and arr.longitude is not None
            ):
                tilt = arr.tilt_deg if arr.tilt_deg is not None else forecasting.default_tilt_deg(arr.latitude)
                az = arr.azimuth_deg if arr.azimuth_deg is not None else forecasting.DEFAULT_AZIMUTH_DEG
                poa = poa_for(arr.latitude, arr.longitude, tilt, az)

            row = build_array_verification(
                db, arr,
                window_days=window_days,
                today=today,
                threshold=thr,
                _poa_by_day=poa,
            )
            if not row.get("available"):
                skipped.append({
                    "array_id": arr.id,
                    "array_name": arr.name,
                    "reason": row.get("reason"),
                })
                continue
            arrays_out.append(row)
            fleet_act += float(row.get("actual_kwh") or 0)
            fleet_exp_m += float(row.get("expected_matched_kwh") or 0)
            priorities.append(float((row.get("deviation") or {}).get("priority") or 0))

    pi = (fleet_act / fleet_exp_m) if fleet_exp_m > 0 else None
    # Rank by priority
    ranked = sorted(
        arrays_out,
        key=lambda r: -float((r.get("deviation") or {}).get("priority") or 0),
    )

    return {
        "available": bool(arrays_out),
        "tenant_id": tenant.id,
        "window_days": window_days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "threshold": thr,
        "portfolio": {
            "array_count": len(arrays_out),
            "skipped_count": len(skipped),
            "actual_kwh": round(fleet_act, 1),
            "expected_matched_kwh": round(fleet_exp_m, 1),
            "performance_index": round(pi, 3) if pi is not None else None,
            "ratio_pct": round(pi * 100) if pi is not None else None,
            "max_priority": max(priorities) if priorities else 0.0,
        },
        "arrays": ranked,
        "skipped": skipped,
        "method": METHOD_SUMMARY,
        "standards_note": IEC_ALIGNMENT_NOTE,
        "report_footer": REPORT_FOOTER,
    }


def calendar_month_window(period: str | None = None, *, today: Optional[date] = None) -> tuple[date, date, str]:
    """Return (start, end, period_label) for a full calendar month.

    period: 'YYYY-MM'. Default = previous complete month relative to today.
    """
    today = today or datetime.utcnow().date()
    if period:
        y, m = period.split("-")
        y_i, m_i = int(y), int(m)
        start = date(y_i, m_i, 1)
        if m_i == 12:
            end = date(y_i + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(y_i, m_i + 1, 1) - timedelta(days=1)
        return start, end, f"{y_i:04d}-{m_i:02d}"

    first_this = today.replace(day=1)
    end = first_this - timedelta(days=1)
    start = end.replace(day=1)
    return start, end, f"{start.year:04d}-{start.month:02d}"


def build_month_verification(
    tenant: Tenant,
    *,
    period: str | None = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Verification pack for a full calendar month (report period).

    Reuses portfolio builder by setting today = end+1 and window = days in month.
    """
    start, end, label = calendar_month_window(period, today=today)
    window_days = (end - start).days + 1
    # build_* uses today-1 as end of window → pass today = end + 1 day
    synth_today = end + timedelta(days=1)
    snap = build_portfolio_verification(
        tenant, window_days=window_days, today=synth_today
    )
    snap["period"] = label
    snap["period_start"] = start.isoformat()
    snap["period_end"] = end.isoformat()
    return snap
