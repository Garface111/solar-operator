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

VENDOR-AGNOSTIC BY ONE CALL PER SITE (the scaling fix)
The poll path now reuses the SAME entrypoint the dashboard cards use:
inverters.fetch_live(vendor, config) — ONE site-level HTTP call that returns the
site's current AC power, dispatched by vendor. This replaces the previous
per-inverter path (1 inventory call + N equipment calls per site), which blew
SolarEdge's 300-req/day/key budget after only a few ticks on a 60-inverter
fleet. SolarEdge works today; SMA/Fronius/Locus/AlsoEnergy activate the moment
an array holds their creds — because fetch_live already dispatches them. So
adding a vendor to the hub = giving an array real creds, not editing this file.

Site power is ALLOCATED across the site's inverters by nameplate share (equal
split when nameplate is unknown). TRADEOFF (stated honestly): a single site-level
reading cannot distinguish which individual inverter is down — per-inverter fault
detection still runs in the on-demand fleet-tree path (inverter_fleet), which the
owner triggers when they open the dashboard. The hub's job is the continuous
"is the site producing right now, and how much" signal at a sustainable API cost;
that is exactly what the night-stale honesty fix needs.

BUDGET GOVERNOR (never burn the 300/day cap)
SolarEdge rate-limits per API key (~300 req/day). One fetch_live = one call, but
a key may cover several sites and the sun can be up ~16h. A per-CREDENTIAL
governor spaces each key's polls so its daily calls can't exceed
DAILY_BUDGET_PER_KEY even in the longest day, and a hard ceiling backstops it.
Cadence is therefore ADAPTIVE: a key with one site polls ~every 5 min; a key
covering many sites polls each one less often, automatically.

SAFETY
- Daylight-gated: skip the whole poll when the sun is down (no API spend at
  night, and night readings are ~0 anyway).
- Per-array isolation: one array's vendor erroring never aborts the run.
- Per-credential budget governor + hard daily ceiling (SolarEdge 300/day safe).
- Bounded writes: only inverters of a site with a real power reading get a row.
- Rolling prune keeps inverter_readings bounded (the scheduler calls
  prune_old_readings daily).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, delete
from sqlalchemy.orm.attributes import flag_modified

from .db import SessionLocal
from .models import (
    Array, Inverter, InverterReading, InverterConnection, now,
)


def _persist_config_if_changed(conn, cfg: dict) -> None:
    """Write an in-place-mutated config back to the connection row.

    OAuth vendors (SMA) rotate their refresh_token on each grant and write the
    new value into the cfg dict in place. JSON columns don't auto-detect nested
    mutation, so re-assign + flag_modified to guarantee the new token is saved.
    Cheap and idempotent — a no-op when nothing changed (same dict reference is
    re-flagged at most once per poll). The surrounding poll commits the session.
    """
    if not isinstance(conn, InverterConnection):
        return
    try:
        if conn.config is not cfg:
            conn.config = cfg
        flag_modified(conn, "config")
    except Exception:  # never let persistence break the poll
        pass

from . import inverter_fleet as _fleet
from . import inverters as _vendors

log = logging.getLogger("poller")

# How long an inverter_readings row is retained. Daily kWh is rolled up into
# InverterDaily independently, so this only bounds the high-frequency series.
READINGS_KEEP_DAYS = 14

# ── budget governor ───────────────────────────────────────────────────────────
# SolarEdge caps at ~300 requests/day per API key. We poll site-level (one
# fetch_live call per site per poll), but a key may cover several sites and the
# sun can be up ~16h. We keep a headroom margin under 300 and SPACE each key's
# polls so the day's calls fit the budget no matter how long it's light.
DAILY_BUDGET_PER_KEY = 280
# Longest plausible daylight span to size spacing against (so the budget is
# guaranteed to last even at summer solstice). Spacing is derived from this, the
# budget, and how many sites share the key — so cadence is automatically adaptive.
_MAX_DAYLIGHT_SECONDS = 16 * 3600

# Per-credential governor state: cred_key -> {"day", "calls", "last_poll"}.
# Module-scoped (the scheduler runs single-process on Railway, matching the
# existing in-memory caches in inverter_fleet/array_owners). A redeploy resets
# it — harmless, the ceiling just re-applies from zero for the rest of the day.
_budget_state: dict[str, dict] = {}


def _reset_budget() -> None:
    """Clear governor state (tests call this for isolation)."""
    _budget_state.clear()


def _credential_key(vendor: str, cfg: dict) -> str:
    """A stable id for the rate-limited PRINCIPAL behind a connection. SolarEdge
    limits per api_key; Fronius per access_key_id; SMA/OAuth vendors per
    client_id. Sites sharing the same key share one budget."""
    v = (vendor or "").lower()
    if v == "solaredge":
        return "solaredge:" + str(cfg.get("api_key") or "")
    if v == "fronius":
        return "fronius:" + str(cfg.get("access_key_id") or "")
    if cfg.get("client_id"):
        return f"{v}:{cfg.get('client_id')}"
    # last resort: whatever uniquely identifies the creds
    return f"{v}:{cfg.get('api_key') or cfg.get('refresh_token') or cfg.get('site_id') or ''}"


def _min_interval_seconds(sites_under_key: int) -> float:
    """Minimum seconds between polls of a credential so its daily calls fit the
    budget across the longest daylight span. More sites under one key → longer
    spacing per site (adaptive cadence)."""
    return _MAX_DAYLIGHT_SECONDS * max(sites_under_key, 1) / DAILY_BUDGET_PER_KEY


def _governor(cred_key: str, day) -> dict:
    st = _budget_state.get(cred_key)
    if st is None or st["day"] != day:
        st = {"day": day, "calls": 0, "last_poll": None}
        _budget_state[cred_key] = st
    return st


def _governor_allows(cred_key: str, day, ts, sites_under_key: int) -> bool:
    """True if this credential may make ONE more site call right now: under the
    hard daily ceiling AND past the minimum spacing interval."""
    st = _governor(cred_key, day)
    if st["calls"] + 1 > DAILY_BUDGET_PER_KEY:
        return False
    last = st["last_poll"]
    if last is not None:
        elapsed = (ts - last).total_seconds()
        if elapsed < _min_interval_seconds(sites_under_key):
            return False
    return True


def _governor_record(cred_key: str, day, ts) -> None:
    st = _governor(cred_key, day)
    st["calls"] += 1
    st["last_poll"] = ts


# ── connection resolution ─────────────────────────────────────────────────────

def _pullable_connection(db, arr: Array):
    """Return the array's PULLABLE connection (one we can fetch server-side with
    stored creds), or None. Reuses fleet._resolve_connection, then requires the
    vendor to support a live pull AND the creds that path needs. Extension-only
    arrays (no pullable creds) → None and are skipped here."""
    conn = _fleet._resolve_connection(db, arr)
    if conn is None:
        return None
    module = _vendors.VENDORS.get((conn.vendor or "").lower())
    if module is None or not getattr(module, "SUPPORTS_LIVE", False):
        return None
    cfg = conn.config or {}
    # SolarEdge / any api-key+site vendor: directly pullable now.
    if cfg.get("api_key") and cfg.get("site_id"):
        return conn
    # Fronius Solar.web Query API: AccessKeyId + AccessKeyValue, scoped to one
    # pv_system_id (see api/inverters/fronius.py). Was missing here entirely —
    # every official-API Fronius connection (Bruce's real Waterford/Chester
    # key) silently never got server-polled despite fetch_live working fine.
    if cfg.get("access_key_id") and cfg.get("access_key_value"):
        return conn
    # SMA / OAuth vendors: pullable once an app/refresh credential is present.
    if cfg.get("client_id") and cfg.get("client_secret"):
        return conn
    if cfg.get("refresh_token"):
        return conn
    return None


def _site_inverters(db, arr: Array, conn) -> list[Inverter]:
    """The PHYSICAL inverters fed by this site's connection, so site-level power
    is allocated to the right hardware even after the owner drags an inverter to
    a different array. Prefer source_connection_id, then source_array_id, then
    the owner array_id (covers freshly-seeded / legacy rows)."""
    cid = getattr(conn, "id", None)
    base = select(Inverter).where(
        Inverter.tenant_id == arr.tenant_id, Inverter.deleted_at.is_(None)
    )
    if cid:
        rows = db.execute(base.where(Inverter.source_connection_id == cid)).scalars().all()
        if rows:
            return rows
    rows = db.execute(base.where(Inverter.source_array_id == arr.id)).scalars().all()
    if rows:
        return rows
    return db.execute(base.where(Inverter.array_id == arr.id)).scalars().all()


def _allocate_power(inverters: list[Inverter], site_power_w) -> dict[int, float]:
    """Split a site's instantaneous AC power across its inverters by NAMEPLATE
    share (the best proxy for how a healthy site's output divides). Falls back to
    an equal split when no nameplate is known. Returns {inverter_id: watts}."""
    n = len(inverters)
    if n == 0 or site_power_w is None:
        return {}
    total_np = sum((iv.nameplate_kw or 0.0) for iv in inverters)
    out: dict[int, float] = {}
    if total_np > 0:
        for iv in inverters:
            out[iv.id] = site_power_w * (iv.nameplate_kw or 0.0) / total_np
    else:
        for iv in inverters:
            out[iv.id] = site_power_w / n
    return out


def poll_all_sources(*, force_daylight: bool | None = None) -> dict:
    """Poll every array with a pullable vendor connection via ONE site-level
    fetch_live call each (budget-governed), allocate the site power across its
    inverters by nameplate share, write InverterReading rows, and refresh
    Inverter.last_power_w/at. Returns a run summary.

    force_daylight: override the daylight gate (True=poll anyway, used by tests /
    manual kicks; None=use the real sun check)."""
    daylight = _fleet._is_daylight() if force_daylight is None else force_daylight
    summary = {
        "ran": True, "daylight": bool(daylight),
        "arrays_polled": 0, "arrays_skipped": 0, "arrays_throttled": 0,
        "inverters_updated": 0, "readings_written": 0, "api_calls": 0,
        "errors": [],
    }
    if not daylight:
        # Sun is down: nothing produces, no API spend. The honesty layer in
        # build_fleet_tree shows "sleeping"/0 from is_daylight regardless.
        summary["ran"] = False
        return summary

    ts = now()
    day = ts.date()
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.deleted_at.is_(None)).order_by(Array.id)
        ).scalars().all()

        # First pass: resolve pullable connections and count sites per credential
        # so the governor can size spacing (cadence) adaptively.
        entries = []  # (arr, conn, cfg, cred_key)
        sites_per_key: dict[str, int] = {}
        for arr in arrays:
            conn = _pullable_connection(db, arr)
            if conn is None:
                summary["arrays_skipped"] += 1
                continue
            cfg = conn.config or {}
            ck = _credential_key(conn.vendor, cfg)
            entries.append((arr, conn, cfg, ck))
            sites_per_key[ck] = sites_per_key.get(ck, 0) + 1

        for arr, conn, cfg, ck in entries:
            # Budget governor: skip (throttle) this site if its credential has
            # spent its budget or hasn't waited the minimum spacing interval.
            if not _governor_allows(ck, day, ts, sites_per_key[ck]):
                summary["arrays_throttled"] += 1
                continue

            try:
                live = _vendors.fetch_live(conn.vendor, cfg)
            except _vendors.InverterError as exc:  # one bad vendor must not abort
                # The call was attempted → it counts against the budget.
                _governor_record(ck, day, ts)
                summary["api_calls"] += 1
                summary["errors"].append(f"array {arr.id} ({conn.vendor}): {exc}")
                log.warning("poller: fetch_live failed for array %s: %s", arr.id, exc, exc_info=True)
                if isinstance(conn, InverterConnection):
                    conn.last_error = str(exc)[:500]
                    conn.status = "error"
                    # An OAuth vendor (SMA) may have cleared a dead rotated token
                    # from cfg in place — persist that so we don't keep retrying it.
                    _persist_config_if_changed(conn, cfg)
                continue

            _governor_record(ck, day, ts)
            summary["api_calls"] += 1

            # OAuth vendors (SMA) rotate the refresh_token on each grant and write
            # the new one back into cfg in place. Persist it now so it survives the
            # access-token expiry AND a redeploy — otherwise the plant goes dark
            # until a manual reconnect (the bug this fixes).
            _persist_config_if_changed(conn, cfg)

            site_power_w = (live or {}).get("current_power_w")
            if site_power_w is None:
                # No usable reading this tick (vendor returned null power).
                summary["arrays_skipped"] += 1
                continue
            site_power_w = round(float(site_power_w), 1)

            invs = _site_inverters(db, arr, conn)
            alloc = _allocate_power(invs, site_power_w)
            polled_any = False
            for iv in invs:
                pw = alloc.get(iv.id)
                if pw is None:
                    continue
                pw = round(float(pw), 1)
                iv.last_power_w = pw
                iv.last_power_at = ts
                iv.last_seen_at = ts
                # Server-polled every ~5 min; the site total is split across ALL
                # units the same way each poll, so there's no partial-capture
                # mismatch — trusted for live anomaly detection (unlike the
                # extension per-device fallback fill). See inverter_alert_sweep.
                iv.last_power_estimated = False
                db.add(InverterReading(
                    tenant_id=iv.tenant_id, inverter_id=iv.id, ts=ts,
                    power_w=pw, energy_today_kwh=None,
                    status="ok", source="poll",
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
                # Got a site reading but no inverters to attach it to (discovery
                # hasn't run yet). Not an error; nothing to write.
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
