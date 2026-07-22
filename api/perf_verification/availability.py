"""Availability KPIs — all-in vs in-service (IEC 61724-3 language).

We do not invent inverter uptime telemetry. When peer/status history is missing,
availability fields are null with a reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence


@dataclass
class AvailabilityKPI:
    window_days: int
    all_in_energy_kwh: Optional[float]
    in_service_energy_kwh: Optional[float]
    downtime_days: Optional[int]
    availability_pct: Optional[float]
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "all_in_energy_kwh": (
                round(self.all_in_energy_kwh, 1)
                if self.all_in_energy_kwh is not None else None
            ),
            "in_service_energy_kwh": (
                round(self.in_service_energy_kwh, 1)
                if self.in_service_energy_kwh is not None else None
            ),
            "downtime_days": self.downtime_days,
            "availability_pct": (
                round(self.availability_pct, 1)
                if self.availability_pct is not None else None
            ),
            "reason": self.reason,
        }


def compute_availability(
    *,
    window_days: int,
    daily: Sequence[dict],
    # daily items: actual_kwh, expected_kwh, status? (ok|fault|dead|comm_gap|None)
) -> AvailabilityKPI:
    if window_days <= 0:
        return AvailabilityKPI(0, None, None, None, None, reason="invalid_window")

    actuals = []
    in_service = []
    downtime = 0
    status_seen = False

    for row in daily:
        act = row.get("actual_kwh")
        st = row.get("status")
        if st:
            status_seen = True
        if act is None:
            continue
        try:
            a = float(act)
        except (TypeError, ValueError):
            continue
        actuals.append(a)
        if st in ("dead", "comm_gap", "fault"):
            downtime += 1
        else:
            in_service.append(a)

    if not actuals:
        return AvailabilityKPI(
            window_days, None, None, None, None,
            reason="no_measured_days",
        )

    all_in = sum(actuals)
    if not status_seen:
        # Energy-only fallback: treat zero-production days with positive expected
        # as potential downtime (weak signal)
        weak_down = 0
        for row in daily:
            act = row.get("actual_kwh")
            exp = row.get("expected_kwh")
            if act is None or exp is None:
                continue
            try:
                if float(act) <= 0 and float(exp) > 1.0:
                    weak_down += 1
            except (TypeError, ValueError):
                continue
        avail = 100.0 * (1.0 - weak_down / max(window_days, 1))
        return AvailabilityKPI(
            window_days=window_days,
            all_in_energy_kwh=all_in,
            in_service_energy_kwh=all_in,  # no status split
            downtime_days=weak_down if weak_down else 0,
            availability_pct=max(0.0, min(100.0, avail)),
            reason="energy_proxy_no_status",
        )

    in_svc_e = sum(in_service)
    avail = 100.0 * (1.0 - downtime / max(window_days, 1))
    return AvailabilityKPI(
        window_days=window_days,
        all_in_energy_kwh=all_in,
        in_service_energy_kwh=in_svc_e,
        downtime_days=downtime,
        availability_pct=max(0.0, min(100.0, avail)),
        reason=None,
    )
