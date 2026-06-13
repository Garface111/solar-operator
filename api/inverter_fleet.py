"""Owner-arrangeable inverter fleet — the backend that makes the sandbox a real
control surface (not just a saved pixel layout).

THE MODEL (why this exists): a solar owner does not think in vendor "sites". They
think in the physical reality on their property — "the six inverters at
Londonderry". The vendor's site grouping is an installer artifact. This module
lets the owner reproduce THEIR mental model: persisted `Inverter` rows whose
`array_id` (the owner grouping) is freely reassignable by dragging, while the
telemetry SOURCE (vendor + site + serial) stays fixed because that's just where
the data physically comes from.

Moving an inverter to a different array genuinely changes its peer cohort, its
reports, and its per-array rollups — front end and back end are one system.

Responsibilities:
  * discover_and_persist  — pull live inventory per connection, upsert Inverter
                            rows idempotently (NEVER clobber owner array_id).
  * _telemetry_for_site   — cached per-site telemetry (respects SolarEdge budget).
  * build_fleet_tree      — read persisted inverters grouped the OWNER's way,
                            attach telemetry by source, peer-analyze each owner
                            group, assemble the 3-tier columns.
  * reassign_inverter / create_array / reset_layout — the mutations a drag drives.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import Array, Inverter, InverterConnection, Tenant, now
from .inverters import peer_analysis

log = logging.getLogger(__name__)

# Per-site telemetry cache (inventory + N equipment calls is heavy; SolarEdge is
# 300 req/day). Keyed by "vendor:site" -> (fetched_at, {serial: row}).
_SITE_TTL = timedelta(minutes=10)
_site_cache: dict[str, tuple] = {}


def _resolve_connection(db, arr: Array):
    """The array's inverter connection (real row, or virtual from legacy
    Array.solaredge_* columns). Mirrors array_owners._resolve_connection."""
    from types import SimpleNamespace
    conn = db.execute(
        select(InverterConnection).where(InverterConnection.array_id == arr.id)
    ).scalar_one_or_none()
    if conn is not None:
        return conn
    if arr.solaredge_api_key and arr.solaredge_site_id:
        return SimpleNamespace(
            id=None, vendor="solaredge",
            config={"api_key": arr.solaredge_api_key, "site_id": arr.solaredge_site_id},
            status="ok",
        )
    return None


# ─────────────────────────── telemetry (by source) ───────────────────────────

def _telemetry_for_site(vendor: str, api_key: str, site_id, *, force: bool = False) -> dict:
    """Return {serial: {name, model, nameplate_kw, daily, error_code, last_report,
    last_mode, last_power_w}} for one source site. Cached 10 min. SolarEdge only
    today; other vendors return {} until their per-inverter capture lands."""
    ck = f"{vendor}:{site_id}"
    if not force:
        hit = _site_cache.get(ck)
        if hit and (now() - hit[0]) < _SITE_TTL:
            return hit[1]

    out: dict[str, dict] = {}
    if vendor == "solaredge":
        from .adapters import solaredge as _se
        try:
            inv = _se.fetch_inventory(api_key, int(site_id))
        except _se.SolarEdgeError as exc:
            log.warning("fleet: inventory fetch failed for site %s: %s", site_id, exc)
            return _site_cache.get(ck, (None, {}))[1] if ck in _site_cache else {}
        for it in inv:
            sn = it.get("sn")
            if not sn:
                continue
            try:
                tel = _se.fetch_inverter_telemetry(api_key, int(site_id), sn, days_back=7)
            except _se.SolarEdgeError:
                tel = {"daily": [], "error_code": None, "last_report": None,
                       "last_mode": None, "last_power_w": None}
            out[str(sn)] = {
                "name": it.get("name"), "model": it.get("model"),
                "nameplate_kw": it.get("nameplate_kw"),
                "daily": tel["daily"], "error_code": tel["error_code"],
                "last_report": tel["last_report"], "last_mode": tel.get("last_mode"),
                "last_power_w": tel.get("last_power_w"),
            }
    _site_cache[ck] = (now(), out)
    return out


# ─────────────────────────── discovery / persistence ─────────────────────────

def discover_and_persist(db, tenant: Tenant, *, force_refresh: bool = False) -> list[Inverter]:
    """Walk every array's connection, pull live inventory, and upsert one
    persisted Inverter per real serial. IDEMPOTENT and owner-safe:

      * keyed by (tenant_id, vendor, serial)
      * NEW serials are created under the Array that owns their source site
        (the owner's starting point = the discovered grouping)
      * EXISTING rows refresh name/model/nameplate/last_seen but KEEP the owner's
        array_id + position (their arrangement is sacred)
      * undeleted if they reappear after a soft-delete

    Returns the tenant's live (non-deleted) inverters.
    """
    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
    ).scalars().all()

    existing = {
        (iv.vendor, iv.serial): iv
        for iv in db.execute(
            select(Inverter).where(Inverter.tenant_id == tenant.id)
        ).scalars().all()
    }

    for arr in arrays:
        conn = _resolve_connection(db, arr)
        if conn is None:
            continue
        vendor = conn.vendor
        cfg = conn.config or {}
        api_key, site_id = cfg.get("api_key"), cfg.get("site_id")
        if not (api_key and site_id):
            continue
        tel = _telemetry_for_site(vendor, api_key, site_id, force=force_refresh)
        for serial, m in tel.items():
            key = (vendor, str(serial))
            iv = existing.get(key)
            if iv is None:
                # Find the next position under this (source) array.
                maxpos = db.execute(
                    select(Inverter.position).where(
                        Inverter.tenant_id == tenant.id,
                        Inverter.array_id == arr.id,
                        Inverter.deleted_at.is_(None),
                    ).order_by(Inverter.position.desc())
                ).scalars().first()
                iv = Inverter(
                    tenant_id=tenant.id,
                    array_id=arr.id,                 # owner grouping starts = source
                    position=(maxpos or 0) + 1,
                    vendor=vendor, serial=str(serial),
                    source_site_id=str(site_id),
                    source_connection_id=getattr(conn, "id", None),
                    source_array_id=arr.id,
                )
                db.add(iv)
                existing[key] = iv
            else:
                # Refresh source pointers in case the connection moved, but DO NOT
                # touch array_id/position (owner's layout).
                iv.source_site_id = str(site_id)
                iv.source_connection_id = getattr(conn, "id", None)
                iv.source_array_id = iv.source_array_id or arr.id
                if iv.deleted_at is not None:
                    iv.deleted_at = None
            # metadata refresh (cheap, safe)
            iv.name = m.get("name") or iv.name or str(serial)
            iv.model = m.get("model") or iv.model
            if m.get("nameplate_kw") is not None:
                iv.nameplate_kw = m.get("nameplate_kw")
            iv.last_seen_at = now()

    db.commit()
    return db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id, Inverter.deleted_at.is_(None)
        ).order_by(Inverter.array_id, Inverter.position)
    ).scalars().all()


# ─────────────────────────────── fleet tree ──────────────────────────────────

_ALERT_HEADLINE = {
    "fault": "Inverter fault — service drafted",
    "dead": "An inverter stopped earning",
    "comm_gap": "An inverter went quiet",
    "underperforming": "A money leak caught early",
    "ok": "All clear",
}
_ALERT_PRIORITY = {"fault": 4, "dead": 4, "comm_gap": 3, "underperforming": 2, "ok": 0}


def _array_alert(inv_rows: list[dict]) -> dict:
    worst, worst_rank, bad = "ok", 0, 0
    for inv in inv_rows:
        st = inv.get("status") or "ok"
        r = _ALERT_PRIORITY.get(st, 0)
        if r >= 2:
            bad += 1
        if r > worst_rank:
            worst_rank, worst = r, st
    level = "critical" if worst_rank >= 4 else "warn" if worst_rank >= 2 else "ok"
    return {"level": level, "count": bad, "status": worst,
            "headline": _ALERT_HEADLINE.get(worst, "All clear")}


def build_fleet_tree(db, tenant: Tenant, *, force_refresh: bool = False) -> dict:
    """Owner-grouped 3-tier tree. Inverters are read from the persisted table
    (owner's arrangement), telemetry pulled from each one's SOURCE site, then
    peer-analyzed WITHIN each owner array group — so a drag changes real cohorts.
    """
    inverters = discover_and_persist(db, tenant, force_refresh=force_refresh)

    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
        .order_by(Array.id)
    ).scalars().all()
    array_by_id = {a.id: a for a in arrays}

    # Group persisted inverters by their OWNER array_id.
    by_array: dict[int, list[Inverter]] = defaultdict(list)
    for iv in inverters:
        by_array[iv.array_id].append(iv)

    columns: list[dict] = []
    inv_total = 0
    for arr in arrays:
        ivs = sorted(by_array.get(arr.id, []), key=lambda x: (x.position, x.id))

        # Pull telemetry per source site (cached), then build peer-units for THIS
        # owner group (cohort = the inverters the owner placed under this array).
        units = []
        meta_by_serial = {}
        for iv in ivs:
            conn_vendor = iv.vendor
            # find the source connection's creds
            src_arr = array_by_id.get(iv.source_array_id) or arr
            conn = _resolve_connection(db, src_arr)
            tel_map = {}
            if conn is not None and (conn.config or {}).get("api_key") and (conn.config or {}).get("site_id"):
                tel_map = _telemetry_for_site(conn_vendor, conn.config["api_key"],
                                              conn.config["site_id"], force=force_refresh)
            m = tel_map.get(iv.serial, {})
            meta_by_serial[iv.serial] = m
            units.append({
                "id": iv.serial,
                "nameplate_kw": iv.nameplate_kw if iv.nameplate_kw is not None else m.get("nameplate_kw"),
                "daily": m.get("daily", []),
                "error_code": m.get("error_code"),
                "last_report": m.get("last_report"),
            })

        analyzed = peer_analysis.analyze_cohort(units) if units else {"units": []}
        an_by_id = {u["id"]: u for u in analyzed["units"]}

        inv_rows = []
        for iv in ivs:
            u = an_by_id.get(iv.serial, {})
            m = meta_by_serial.get(iv.serial, {})
            inv_rows.append({
                "inverter_id": iv.id,
                "sn": iv.serial,
                "name": iv.name or m.get("name") or iv.serial,
                "model": iv.model or m.get("model"),
                "vendor": iv.vendor,
                "nameplate_kw": iv.nameplate_kw if iv.nameplate_kw is not None else m.get("nameplate_kw"),
                "peer_index": u.get("peer_index"),
                "status": u.get("status", "ok"),
                "diagnosis": u.get("diagnosis"),
                "window_kwh": u.get("window_kwh"),
                "last_mode": m.get("last_mode"),
                "current_power_w": m.get("last_power_w"),
                "last_report": u.get("last_report") or m.get("last_report"),
                "source_array_id": iv.source_array_id,
                "moved": iv.source_array_id is not None and iv.source_array_id != iv.array_id,
            })
        inv_total += len(inv_rows)

        # vendor mix for the array chip
        vendors = sorted({iv.vendor for iv in ivs})
        columns.append({
            "array_id": arr.id,
            "array_name": arr.name,
            "vendor": vendors[0] if len(vendors) == 1 else None,
            "vendors": vendors,
            "inverter_source": "live" if ivs else None,
            "inverter_count": len(inv_rows),
            "alert": _array_alert(inv_rows),
            "inverters": inv_rows,
        })

    attention = sum(c["alert"]["count"] for c in columns)
    return {
        "generated_at": now().replace(microsecond=0).isoformat() + "Z",
        "tiers": ["alerts", "arrays", "inverters"],
        "columns": columns,
        "summary": {
            "arrays_total": len(columns),
            "inverters_total": inv_total,
            "attention": attention,
        },
    }


# ─────────────────────────────── mutations ───────────────────────────────────

class FleetError(Exception):
    """Raised for invalid owner mutations (bad ids, cross-tenant, etc.)."""


def reassign_inverter(db, tenant: Tenant, inverter_id: int, target_array_id: int,
                      position: Optional[int] = None) -> Inverter:
    """Move an inverter to a different array (the owner's drag). Telemetry source
    is untouched — only the owner grouping changes. Re-sequences positions."""
    iv = db.get(Inverter, inverter_id)
    if iv is None or iv.tenant_id != tenant.id or iv.deleted_at is not None:
        raise FleetError("Inverter not found")
    target = db.get(Array, target_array_id)
    if target is None or target.tenant_id != tenant.id or target.deleted_at is not None:
        raise FleetError("Target array not found")

    iv.array_id = target_array_id
    # place at end unless a position is given
    if position is None:
        maxpos = db.execute(
            select(Inverter.position).where(
                Inverter.tenant_id == tenant.id, Inverter.array_id == target_array_id,
                Inverter.deleted_at.is_(None), Inverter.id != iv.id,
            ).order_by(Inverter.position.desc())
        ).scalars().first()
        iv.position = (maxpos or 0) + 1
    else:
        iv.position = int(position)
    db.commit()
    db.refresh(iv)
    return iv


def reorder_within_array(db, tenant: Tenant, array_id: int, ordered_ids: list[int]) -> None:
    """Persist the order of inverters within one array (drag-to-reorder)."""
    arr = db.get(Array, array_id)
    if arr is None or arr.tenant_id != tenant.id:
        raise FleetError("Array not found")
    pos = 1
    for iid in ordered_ids:
        iv = db.get(Inverter, iid)
        if iv is not None and iv.tenant_id == tenant.id and iv.array_id == array_id:
            iv.position = pos
            pos += 1
    db.commit()


def create_array(db, tenant: Tenant, name: str) -> Array:
    """Create a new owner-defined array (empty group to drag inverters into).
    No utility/connection — purely an owner grouping that inverters reference.

    Array names are unique per tenant (uq_array_per_tenant), so if the requested
    name collides we auto-suffix (" 2", " 3", …) rather than 500. Also revives a
    soft-deleted array of the same name instead of colliding with its row."""
    nm = (name or "").strip() or "New array"

    # Revive a soft-deleted same-name array if one exists (the unique constraint
    # spans deleted rows too, so we can't just insert a duplicate).
    existing = db.execute(
        select(Array).where(Array.tenant_id == tenant.id, Array.name == nm)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is not None:
            existing.deleted_at = None
            db.commit()
            db.refresh(existing)
            return existing
        # live array already has this name — auto-suffix to keep it unique
        base = nm
        for i in range(2, 100):
            cand = f"{base} {i}"
            clash = db.execute(
                select(Array).where(Array.tenant_id == tenant.id, Array.name == cand)
            ).scalar_one_or_none()
            if clash is None:
                nm = cand
                break

    arr = Array(tenant_id=tenant.id, name=nm, fuel_type="solar")
    db.add(arr)
    db.commit()
    db.refresh(arr)
    return arr


def reset_layout(db, tenant: Tenant) -> int:
    """Snap every inverter back to its discovered (source) array grouping.
    Returns count reset. Empty owner-created arrays are left in place."""
    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id, Inverter.deleted_at.is_(None)
        )
    ).scalars().all()
    n = 0
    for iv in invs:
        if iv.source_array_id and iv.array_id != iv.source_array_id:
            iv.array_id = iv.source_array_id
            n += 1
    db.commit()
    return n
