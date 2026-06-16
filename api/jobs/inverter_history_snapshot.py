"""Daily per-inverter history snapshot.

The per-inverter graphs read from the InverterDaily store so they survive even when
a vendor API is slow/down/off-peak (see inverter_fleet._merged_daily). build_fleet_tree
persists whatever daily readings it sees as a side effect (persist-on-read) — but that
only fires when someone OPENS the dashboard. This job forces that capture on a schedule
so history keeps accumulating for every owner regardless of dashboard traffic.

It simply runs build_fleet_tree(force_refresh=True) for every active Array Operator
tenant; the snapshot-into-InverterDaily happens inside. Per-tenant errors are logged
but never crash the scheduler.

SolarEdge is the key beneficiary: its per-inverter telemetry is otherwise live-only and
never persisted, so without this its graphs vanish whenever the API doesn't answer.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant

log = logging.getLogger(__name__)


def snapshot_all_inverter_history() -> dict:
    """Force a fleet-tree build for every active Array Operator tenant so each
    inverter's daily reading is persisted into InverterDaily. Returns a summary.

    Called by the scheduler daily (after the inverter pull). Idempotent: re-running
    the same day just upserts the day's kWh (keeps the larger value)."""
    from ..inverter_fleet import build_fleet_tree

    tenants_done = 0
    inverters_seen = 0
    errors: list[str] = []

    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(
                Tenant.product == "array_operator",
                Tenant.active.is_(True),
            )
        ).scalars().all()

    for t in tenants:
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, t.id)
                if tenant is None:
                    continue
                tree = build_fleet_tree(db, tenant, force_refresh=True)
                inverters_seen += tree.get("summary", {}).get("inverters_total", 0)
                tenants_done += 1
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            log.warning("snapshot_inverter_history tenant=%s error=%s", t.id, exc)
            errors.append(f"{t.id}: {exc}")

    return {
        "tenants_processed": tenants_done,
        "inverters_seen": inverters_seen,
        "errors": errors,
    }
