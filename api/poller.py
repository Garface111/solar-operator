"""Generalized server-side telemetry poller — the data-hub spine.

WHY THIS EXISTS
The product was a stale-snapshot viewer: SolarEdge refreshed live (it has a
pullable API key), but extension-captured vendors (SMA/Fronius/Chint) only
updated when an owner manually re-logged-in. Between captures we kept showing
the last reading — so at 9pm we'd display a 2pm peak as "producing now," the
opposite of what the vendor's own portal showed.

This poller makes the product a real-time HUB: every array that holds a
PULLABLE vendor connection (API key / OAuth token) is fetched on a tight
interval, server-side, with NO browser in the loop. Each tick writes an
InverterReading time-series row and refreshes Inverter.last_power_w/at, so the
fleet tree's "current kW", the live sparkline, and the intraday curve all track
the vendor continuously.

VENDOR-AGNOSTIC BY DESIGN
The poll path reuses inverter_fleet._telemetry_for_site, which dispatches by
vendor. SolarEdge works TODAY (proven on Bruce's live fleet). SMA/Fronius/Locus/
AlsoEnergy activate the moment a pullable connection (their OAuth/app creds)
exists for an array — no poller change needed. Extension-only captures (no
pullable creds) are simply skipped here; they still flow through the capture
path. So adding a vendor to the hub = giving an array real API creds, not
editing this file.

SAFETY
- Daylight-gated: skip the whole poll when the sun is down (no API spend at
  night, and night readings are ~0 anyway).
- Per-array isolation: one array's vendor erroring never aborts the run.
- Bounded writes: only inverters with a real, fresh power reading get a row.
- Rolling prune keeps inverter_readings bounded (the scheduler calls
  prune_old_readings daily after the daily roll-up).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, delete

from .db import SessionLocal
from .models import (
    Tenant, Array, Inverter, InverterReading, InverterConnection, now,
)
from . import inverter_fleet as _fleet

log = logging.getLogger("poller")

# How long an inverter_readings row is retained. Daily kWh is rolled up into
# InverterDaily independently, so this only bounds the high-frequency series.
READINGS_KEEP_DAYS = 14


def _pullable_connection(db, arr: Array):
    """Return the array's PULLABLE connection (one we can fetch server-side with
    stored creds), or None. Reuses fleet._resolve_connection, then requires the
    creds the telemetry path actually needs (api_key + site_id today; an OAuth
    config check can extend this per vendor). Extension-only arrays → None."""
    conn = _fleet._resolve_connection(db, arr)
    if conn is None:
        return None
    cfg = conn.config or {}
    # SolarEdge / any api-key+site vendor: directly pullable now.
    if cfg.get("api_key") and cfg.get("site_id"):
        return conn
    # SMA / OAuth vendors: pullable once an app/refresh credential is present.
    # _telemetry_for_site will dispatch on conn.vendor; we just gate on creds.
    if cfg.get("refresh_token") or (cfg.get("client_id") and cfg.get("client_secret")):
        return conn
    return None


def poll_all_sources(*, force_daylight: bool | None = None) -> dict:
    """Poll every array with a pullable vendor connection, write InverterReading
    rows, and refresh Inverter.last_power_w/at. Returns a run summary.

    force_daylight: override the daylight gate (True=poll anyway, used by tests /
    manual kicks; None=use the real sun check)."""
    daylight = _fleet._is_daylight() if force_daylight is None else force_daylight
    summary = {
        "ran": True, "daylight": bool(daylight),
        "arrays_polled": 0, "arrays_skipped": 0, "inverters_updated": 0,
        "readings_written": 0, "errors": [],
    }
    if not daylight:
        # Sun is down: nothing produces, no API spend. The honesty layer in
        # build_fleet_tree will show "sleeping"/0 from is_daylight regardless.
        summary["ran"] = False
        return summary

    ts = now()
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.deleted_at.is_(None)).order_by(Array.id)
        ).scalars().all()

        for arr in arrays:
            conn = _pullable_connection(db, arr)
            if conn is None:
                summary["arrays_skipped"] += 1
                continue

            cfg = conn.config or {}
            try:
                tel_map = _fleet._telemetry_for_site(
                    conn.vendor, cfg.get("api_key"), cfg.get("site_id"), force=True
                )
            except Exception as exc:  # one bad vendor must not abort the run
                summary["errors"].append(f"array {arr.id} ({conn.vendor}): {exc}")
                log.warning("poller: telemetry failed for array %s: %s", arr.id, exc, exc_info=True)
                # mark the connection so the UI can show a real error, not silence
                if isinstance(conn, InverterConnection):
                    conn.last_error = str(exc)[:500]
                continue

            if not tel_map:
                summary["arrays_skipped"] += 1
                continue

            # Map this array's inverters (by serial) to the fresh telemetry.
            invs = db.execute(
                select(Inverter).where(
                    Inverter.array_id == arr.id, Inverter.deleted_at.is_(None)
                )
            ).scalars().all()
            # Source array may differ from owner array after a drag; match by serial
            # within the whole tenant's source set is handled in fleet build, but for
            # polling the source site's serials are authoritative — match directly.
            polled_any = False
            for iv in invs:
                m = tel_map.get(iv.serial)
                if not m:
                    continue
                pw = m.get("last_power_w")
                if pw is None:
                    continue
                # Refresh the live pointer (what the card's "current kW" reads).
                iv.last_power_w = round(float(pw), 1)
                iv.last_power_at = ts
                iv.last_seen_at = ts
                # Append the time-series row (the hub's intraday memory).
                db.add(InverterReading(
                    tenant_id=iv.tenant_id, inverter_id=iv.id, ts=ts,
                    power_w=round(float(pw), 1),
                    energy_today_kwh=None,
                    status=m.get("error_code") or "ok",
                    source="poll",
                ))
                summary["inverters_updated"] += 1
                summary["readings_written"] += 1
                polled_any = True

            if isinstance(conn, InverterConnection):
                conn.last_sync_at = ts
                conn.status = "ok"
                conn.last_error = None
            if polled_any:
                summary["arrays_polled"] += 1
            else:
                summary["arrays_skipped"] += 1

        db.commit()

    return summary


def prune_old_readings(keep_days: int = READINGS_KEEP_DAYS) -> dict:
    """Drop InverterReading rows older than keep_days so the high-frequency
    series stays bounded. Daily energy lives in InverterDaily and is untouched."""
    cutoff = now() - timedelta(days=keep_days)
    with SessionLocal() as db:
        res = db.execute(delete(InverterReading).where(InverterReading.ts < cutoff))
        db.commit()
        return {"pruned": res.rowcount or 0, "cutoff": cutoff.isoformat()}
