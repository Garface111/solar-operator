"""Post-repair recovery measurement (PI before vs after resolution).

Computes PI for window_days before and after resolved_on using the same
array verification engine. Prefer on-read; never fabricates recovery when
data is thin.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger("perf_verification.intervention")


def measure_recovery(
    db,
    tenant,
    *,
    array_id: int,
    resolved_on: date,
    window_days: int = 14,
) -> dict[str, Any]:
    """Compare PI for `window_days` before vs after a resolution date.

    Returns dict with pi_before, pi_after, recovery_delta, available.
    Safe when insufficient data: available=False + reason.
    """
    from sqlalchemy import select

    from ..models import Array
    from .engine import _threshold_for_tenant, build_array_verification

    base: dict[str, Any] = {
        "available": False,
        "array_id": array_id,
        "resolved_on": (
            resolved_on.isoformat()
            if isinstance(resolved_on, date)
            else str(resolved_on)
        ),
        "window_days": window_days,
        "pi_before": None,
        "pi_after": None,
        "recovery_delta": None,
        "reason": None,
    }

    if window_days is None or int(window_days) < 1:
        base["reason"] = "invalid_window"
        return base
    window_days = int(window_days)

    if isinstance(resolved_on, datetime):
        resolved_on = resolved_on.date()
    if not isinstance(resolved_on, date):
        try:
            resolved_on = date.fromisoformat(str(resolved_on)[:10])
        except (TypeError, ValueError):
            base["reason"] = "invalid_resolved_on"
            return base
    base["resolved_on"] = resolved_on.isoformat()

    try:
        arr = db.execute(
            select(Array).where(
                Array.id == array_id,
                Array.tenant_id == getattr(tenant, "id", None),
                Array.deleted_at.is_(None),
            )
        ).scalars().first()
    except Exception as e:
        log.warning("measure_recovery: array lookup failed: %s", e)
        base["reason"] = f"array_lookup_error: {e}"
        return base

    if arr is None:
        base["reason"] = "array_not_found"
        return base

    thr = _threshold_for_tenant(tenant)

    # Before window ends the day before resolution:
    # build_array_verification uses end = today-1 → today = resolved_on
    before_today = resolved_on
    # After window: [resolved_on, resolved_on + window_days - 1]
    # end = after_today - 1 = resolved_on + window_days - 1
    after_today = resolved_on + timedelta(days=window_days)

    try:
        before = build_array_verification(
            db,
            arr,
            window_days=window_days,
            today=before_today,
            threshold=thr,
        )
    except Exception as e:
        log.warning(
            "measure_recovery: before-window failed array=%s: %s", array_id, e
        )
        base["reason"] = f"before_window_error: {e}"
        return base

    try:
        after = build_array_verification(
            db,
            arr,
            window_days=window_days,
            today=after_today,
            threshold=thr,
        )
    except Exception as e:
        log.warning(
            "measure_recovery: after-window failed array=%s: %s", array_id, e
        )
        base["reason"] = f"after_window_error: {e}"
        return base

    pi_before = before.get("performance_index") if before.get("available") else None
    pi_after = after.get("performance_index") if after.get("available") else None

    base["before"] = {
        "available": bool(before.get("available")),
        "reason": before.get("reason"),
        "performance_index": pi_before,
        "window_start": before.get("window_start"),
        "window_end": before.get("window_end"),
        "measured_days": before.get("measured_days"),
    }
    base["after"] = {
        "available": bool(after.get("available")),
        "reason": after.get("reason"),
        "performance_index": pi_after,
        "window_start": after.get("window_start"),
        "window_end": after.get("window_end"),
        "measured_days": after.get("measured_days"),
    }
    base["pi_before"] = pi_before
    base["pi_after"] = pi_after

    if pi_before is None and pi_after is None:
        base["reason"] = (
            before.get("reason") or after.get("reason") or "insufficient_data"
        )
        return base

    if pi_before is None:
        base["reason"] = "insufficient_before_data"
        return base
    if pi_after is None:
        base["reason"] = "insufficient_after_data"
        return base

    try:
        delta = round(float(pi_after) - float(pi_before), 4)
    except (TypeError, ValueError):
        base["reason"] = "invalid_pi"
        return base

    base["available"] = True
    base["recovery_delta"] = delta
    base["reason"] = None
    return base


def build_intervention_verification(
    tenant,
    repair_ticket_id: int,
    *,
    window_days: int = 14,
) -> dict[str, Any]:
    """Ticket-based wrapper around measure_recovery (optional convenience)."""
    from sqlalchemy import select

    from ..db import SessionLocal
    from ..models import RepairTicket, Tenant

    with SessionLocal() as db:
        t = db.get(Tenant, getattr(tenant, "id", None)) or tenant
        ticket = db.execute(
            select(RepairTicket).where(
                RepairTicket.id == repair_ticket_id,
                RepairTicket.tenant_id == t.id,
            )
        ).scalar_one_or_none()
        if ticket is None:
            return {
                "available": False,
                "reason": "ticket_not_found",
                "repair_ticket_id": repair_ticket_id,
            }
        resolved_at = getattr(ticket, "resolved_at", None) or getattr(
            ticket, "cleared_at", None
        )
        if ticket.array_id is None:
            return {
                "available": False,
                "reason": "no_array",
                "repair_ticket_id": ticket.id,
            }
        if resolved_at is None:
            return {
                "available": False,
                "reason": "not_resolved",
                "repair_ticket_id": ticket.id,
                "array_id": ticket.array_id,
            }
        res_day = (
            resolved_at.date()
            if isinstance(resolved_at, datetime)
            else resolved_at
        )
        out = measure_recovery(
            db,
            t,
            array_id=ticket.array_id,
            resolved_on=res_day,
            window_days=window_days,
        )
        out["repair_ticket_id"] = ticket.id
        out["status"] = getattr(ticket, "status", None)
        out["site_name"] = getattr(ticket, "site_name", None)
        return out
