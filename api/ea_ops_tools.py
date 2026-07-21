"""Energy Agent ops tools: financial health, capture health, documents, underperformer trends.

Called from energy_agent._run_tool — keeps energy_agent.py thinner.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, desc, func

from .models import (
    AgentDocument,
    Array,
    Bill,
    DailyGeneration,
    HarvestRun,
    Inverter,
    InverterDaily,
    PortalCredential,
    PortalLoginStatus,
    RepairTicket,
    Tenant,
    UtilityAccount,
    now,
)

log = logging.getLogger("api.ea_ops_tools")

_LOSS_WINDOW_DAYS = 14
_ENERGY_RATE_FALLBACK = 0.21
_DOC_TYPES = frozenset({
    "warranty_claim", "diagnostic", "service_request", "note", "other",
})


def _iso(dt) -> str | None:
    if not dt:
        return None
    try:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    except Exception:
        return None


def _rate(tenant: Tenant) -> float:
    try:
        r = float(getattr(tenant, "default_net_rate_per_kwh", None) or 0)
    except (TypeError, ValueError):
        r = 0.0
    return r if r > 0 else _ENERGY_RATE_FALLBACK


# ── fleet_financial_health ───────────────────────────────────────────────────

def fleet_financial_health(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Fleet-wide money at risk: $/mo burn, per-array cumulative estimate, recoverable if fixed.

    Reuses the same recoverable math as Fleet Triage / investigate_attention.
    """
    args = args or {}
    from . import energy_agent as ea

    rate = _rate(tenant)
    try:
        columns, _summary = ea._fleet_tree_columns(db, tenant)
    except Exception as e:
        return {"ok": False, "error": f"fleet tree failed: {e}"}

    columns = list(columns or [])
    per_array = []
    total_usd_mo = 0.0
    total_lost_kwh_14d = 0.0
    priced_units = 0

    for col in columns:
        if not isinstance(col, dict):
            continue
        rec = ea._column_recoverable(col, rate)
        usd_mo = float(rec.get("est_loss_usd_month") or 0)
        lost_kwh = float(rec.get("est_lost_kwh_14d") or 0)
        total_usd_mo += usd_mo
        total_lost_kwh_14d += lost_kwh
        priced_units += len(rec.get("priced_inverters") or [])

        # Cumulative loss estimate while issues persist:
        # open repair ticket age × current monthly burn for that array, else 14d window loss.
        arr_id = col.get("array_id") or col.get("id")
        days_open = None
        ticket_rows = []
        if arr_id:
            try:
                open_tix = db.execute(
                    select(RepairTicket).where(
                        RepairTicket.tenant_id == tenant.id,
                        RepairTicket.array_id == int(arr_id),
                        RepairTicket.status.in_(
                            ("open", "waiting_reply", "scheduled", "in_progress")
                        ),
                    )
                ).scalars().all()
                for t in open_tix:
                    age_d = max(1.0, (now() - (t.opened_at or now())).total_seconds() / 86400.0)
                    days_open = max(days_open or 0, age_d)
                    ticket_rows.append({
                        "ticket_id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "fail_type": t.fail_type,
                        "days_open": round(age_d, 1),
                    })
            except Exception:
                pass

        daily_burn = usd_mo / 30.0
        if days_open and daily_burn > 0:
            cumulative = round(daily_burn * days_open, 2)
            cumulative_basis = f"open_ticket_days×daily_burn (~{days_open:.0f}d)"
        else:
            cumulative = round(lost_kwh * rate, 2)
            cumulative_basis = "14d_window_lost_kwh×rate"

        if usd_mo >= 0.5 or lost_kwh >= 1 or ticket_rows:
            per_array.append({
                "array_id": arr_id,
                "name": col.get("name") or col.get("array_name") or "array",
                "usd_per_month_at_risk": round(usd_mo, 2),
                "est_lost_kwh_14d": round(lost_kwh, 1),
                "cumulative_loss_usd_est": cumulative,
                "cumulative_basis": cumulative_basis,
                "recoverable_if_fixed_usd_month": round(usd_mo, 2),
                "priced_inverters": rec.get("priced_inverters") or [],
                "open_tickets": ticket_rows,
            })

    per_array.sort(key=lambda r: -(r.get("usd_per_month_at_risk") or 0))

    return {
        "ok": True,
        "rate_usd_per_kwh": rate,
        "window_days": _LOSS_WINDOW_DAYS,
        "total_usd_per_month_at_risk": round(total_usd_mo, 2),
        "total_est_lost_kwh_14d": round(total_lost_kwh_14d, 1),
        "total_recoverable_if_fixed_usd_month": round(total_usd_mo, 2),
        "priced_inverter_count": priced_units,
        "arrays_with_loss": per_array[:40],
        "note": (
            "Matches Fleet Triage Recoverable tile math (dead/fault/underperforming only; "
            "live dark/low and comm_gap not priced yet). Cumulative is an estimate from "
            "open-ticket age × burn rate when available, else the 14-day window."
        ),
    }


# ── capture_health_detail ────────────────────────────────────────────────────

def _safe_harvest_detail(detail: str | None, *, limit: int = 160) -> str | None:
    """Strip Playwright/HTML/selector dumps before anything reaches an external LLM."""
    if not detail:
        return None
    msg = str(detail)
    for marker in ("Call log:", "<html", "<!DOCTYPE", "Locator.", "waiting for", "\n  - "):
        idx = msg.find(marker)
        if idx > 0:
            msg = msg[:idx]
    msg = " ".join(msg.split())
    if len(msg) > limit:
        msg = msg[:limit] + "…"
    return msg or None


def capture_health_detail(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Per-vendor capture status: last success, last error, why stale.

    Never materializes portal passwords — web runs with SO_VAULT_DECRYPT=0.
    """
    args = args or {}
    provider_filter = (args.get("provider") or args.get("vendor") or "").strip().lower() or None
    from sqlalchemy.orm import load_only

    # Scheduling/meta columns only — EncryptedVault* decrypts on full-entity fetch.
    creds = list(db.execute(
        select(PortalCredential)
        .where(PortalCredential.tenant_id == tenant.id)
        .options(load_only(
            PortalCredential.id,
            PortalCredential.tenant_id,
            PortalCredential.provider,
            PortalCredential.username,
            PortalCredential.username_lc,
            PortalCredential.cloud_capture_enabled,
            PortalCredential.last_harvest_at,
            PortalCredential.last_harvest_ok,
            PortalCredential.harvest_fails,
        ))
    ).scalars().all())
    # has_secret without decrypt
    secret_flags: dict[int, bool] = {}
    if creds:
        from sqlalchemy import bindparam, text as sa_text
        ids = [c.id for c in creds]
        stmt = sa_text(
            "SELECT id, (secret_enc IS NOT NULL) FROM portal_credential WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        for rid, has in db.execute(stmt, {"ids": ids}).fetchall():
            secret_flags[int(rid)] = bool(has)

    statuses = list(db.execute(
        select(PortalLoginStatus).where(PortalLoginStatus.tenant_id == tenant.id)
    ).scalars().all())
    status_by_key = {
        ((s.provider or "").lower(), (s.username_lc or "").lower()): s
        for s in statuses
    }

    vendors = []
    for c in creds:
        prov = (c.provider or "").strip().lower()
        if provider_filter and prov != provider_filter and provider_filter not in prov:
            continue
        uname_lc = (c.username_lc or c.username or "").lower()
        st = status_by_key.get((prov, uname_lc))
        has_secret = secret_flags.get(c.id, False)

        # Latest harvest runs
        runs = list(db.execute(
            select(HarvestRun).where(
                HarvestRun.tenant_id == tenant.id,
                HarvestRun.provider == c.provider,
            ).order_by(desc(HarvestRun.started_at)).limit(5)
        ).scalars().all())
        last_run = runs[0] if runs else None
        last_fail = next((r for r in runs if (r.status or "") not in ("ok", "skipped")), None)
        last_ok_run = next((r for r in runs if r.status == "ok"), None)

        last_ok_at = None
        if last_ok_run and last_ok_run.ended_at:
            last_ok_at = last_ok_run.ended_at
        elif c.last_harvest_ok and c.last_harvest_at:
            last_ok_at = c.last_harvest_at
        elif st and st.last_ok_at:
            last_ok_at = st.last_ok_at

        age_h = None
        if last_ok_at:
            try:
                age_h = max(0.0, (now() - last_ok_at.replace(tzinfo=None)
                                  if getattr(last_ok_at, "tzinfo", None) else now() - last_ok_at
                                  ).total_seconds() / 3600.0)
            except Exception:
                age_h = None

        run_status = (last_run.status if last_run else None) or (
            "ok" if c.last_harvest_ok else ("failed" if c.last_harvest_ok is False else "unknown")
        )
        safe_detail = _safe_harvest_detail(
            (last_run.detail if last_run else None) or (last_fail.detail if last_fail else None),
            limit=200,
        )
        why = _explain_capture_failure(
            run_status=run_status,
            detail=safe_detail,
            harvest_fails=c.harvest_fails or 0,
            paused=bool(st.paused) if st else False,
            enabled=bool(c.cloud_capture_enabled),
            has_secret=has_secret,
            age_h=age_h,
            device_mode=(getattr(tenant, "capture_mode", None) or "").lower() == "device",
        )

        vendors.append({
            "provider": c.provider,
            "username": c.username,
            "cloud_enabled": bool(c.cloud_capture_enabled),
            "has_password_stored": has_secret,
            "last_harvest_at": _iso(c.last_harvest_at),
            "last_harvest_ok": c.last_harvest_ok,
            "harvest_fails": c.harvest_fails or 0,
            "last_success_at": _iso(last_ok_at),
            "hours_since_success": round(age_h, 1) if age_h is not None else None,
            "device_status": {
                "enabled": bool(st.enabled) if st else None,
                "paused": bool(st.paused) if st else None,
                "fails": st.fails if st else None,
                "last_ok_at": _iso(st.last_ok_at) if st else None,
            } if st else None,
            "last_run": {
                "status": last_run.status if last_run else None,
                "started_at": _iso(last_run.started_at) if last_run else None,
                "ended_at": _iso(last_run.ended_at) if last_run else None,
                "detail": _safe_harvest_detail(last_run.detail if last_run else None, limit=200),
                "logged_in_fresh": last_run.logged_in_fresh if last_run else None,
                "rows_written": last_run.rows_written if last_run else None,
            } if last_run else None,
            "last_error": {
                "status": last_fail.status if last_fail else None,
                "at": _iso(last_fail.started_at) if last_fail else None,
                "detail": _safe_harvest_detail(last_fail.detail if last_fail else None, limit=200),
            } if last_fail else None,
            "diagnosis": why,
            "recent_runs": [
                {
                    "status": r.status,
                    "at": _iso(r.started_at),
                    "detail": _safe_harvest_detail(r.detail, limit=120),
                    "rows": r.rows_written,
                }
                for r in runs
            ],
        })

    # Extension heartbeat
    ext_hb = getattr(tenant, "extension_heartbeat_at", None)
    capture_mode = getattr(tenant, "capture_mode", None)

    return {
        "ok": True,
        "capture_mode": capture_mode,
        "extension_heartbeat_at": _iso(ext_hb),
        "vendor_count": len(vendors),
        "vendors": vendors,
        "stale_or_failing": [v for v in vendors if (v.get("diagnosis") or {}).get("severity") in ("critical", "warning")],
        "note": (
            "login_failed = credential/MFA problem; scrape_failed = signed in but data pull hiccuped "
            "(not a password issue); paused device vault = auto-login gave up after fails."
        ),
    }


def _explain_capture_failure(
    *,
    run_status: str | None,
    detail: str | None,
    harvest_fails: int,
    paused: bool,
    enabled: bool,
    has_secret: bool,
    age_h: float | None,
    device_mode: bool,
) -> dict:
    st = (run_status or "unknown").lower()
    d = (detail or "").lower()
    reasons = []
    severity = "ok"

    if not enabled and not device_mode:
        reasons.append("cloud capture disabled for this login")
        severity = "info"
    if not has_secret and enabled:
        reasons.append("no password stored — re-save portal login on Account → Auto-refresh")
        severity = "critical"
    if paused:
        reasons.append("device auto-login paused after repeated failures (likely wrong password)")
        severity = "critical"
    if st == "login_failed":
        reasons.append("portal login failed — password changed, MFA wall, or account lockout")
        severity = "critical"
    elif st == "scrape_failed":
        reasons.append("login succeeded but data scrape failed — not a password problem; will retry")
        severity = "warning"
    elif st in ("error", "failed"):
        reasons.append(f"last run status={st}")
        severity = "warning"
    elif st == "no_creds":
        reasons.append("no credentials available for harvest")
        severity = "critical"
    elif st == "disabled":
        reasons.append("harvest disabled")
        severity = "info"
    if "rate" in d and "limit" in d:
        reasons.append("possible rate limit from vendor portal")
        severity = "warning"
    if "timeout" in d or "timed out" in d:
        reasons.append("portal timed out / offline")
        severity = "warning"
    if "mfa" in d or "2fa" in d or "two-factor" in d:
        reasons.append("MFA / 2FA blocking automated login")
        severity = "critical"
    if harvest_fails and harvest_fails >= 3:
        reasons.append(f"{harvest_fails} consecutive harvest failures")
        if severity == "ok":
            severity = "warning"
    if age_h is not None and age_h > 30:
        reasons.append(f"data ~{age_h:.0f}h old (stale)")
        if severity == "ok":
            severity = "warning"
    if not reasons:
        if st == "ok" or (age_h is not None and age_h < 6):
            reasons.append("healthy")
        else:
            reasons.append("no recent successful harvest recorded")
            severity = "info" if severity == "ok" else severity

    return {
        "severity": severity,
        "summary": "; ".join(reasons),
        "raw_status": st,
        # Already sanitized by callers; hard-cap + re-scrub for defense in depth.
        "raw_detail": _safe_harvest_detail(detail, limit=200),
    }


# ── underperformer_history / steady candidate ────────────────────────────────

def _col_array_name(col: dict) -> str:
    """Fleet-tree columns use array_name (not name) — never invent either."""
    return str(col.get("array_name") or col.get("name") or "")


def _inv_pk(inv: dict) -> int | None:
    for k in ("inverter_id", "id"):
        v = inv.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def underperformer_history(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Peer-index style timeline from daily kWh: steady-low vs recent drop.

    Flags steady_underperformer_candidate when a unit has been consistently low
    for many weeks (shading/orientation) vs a sudden cliff (real fault).

    IMPORTANT: only sets steady_underperformer_candidate when there is real
    multi-week InverterDaily / DailyGeneration history. Short 14d peer index
    alone is NOT enough to claim shading.
    """
    args = args or {}
    from . import energy_agent as ea

    inv_id = args.get("inverter_id")
    array_id = args.get("array_id")
    array_name = (args.get("array_name") or "").strip()
    window_days = int(args.get("window_days") or 365)
    # Allow full multi-year windows (was capped at 365; still clamp for safety)
    window_days = max(30, min(int(window_days), 3650))

    try:
        columns, _summary = ea._fleet_tree_columns(db, tenant)
    except Exception as e:
        return {"ok": False, "error": f"fleet tree failed: {e}"}

    columns = list(columns or [])
    units = []
    for col in columns:
        cname = _col_array_name(col)
        cid = col.get("array_id") or col.get("id")
        if array_id is not None:
            try:
                if int(cid or 0) != int(array_id):
                    continue
            except (TypeError, ValueError):
                continue
        if array_name and array_name.lower() not in cname.lower():
            continue
        for inv in (col.get("inverters") or []):
            iid = _inv_pk(inv)
            if inv_id is not None:
                try:
                    if iid is None or int(iid) != int(inv_id):
                        continue
                except (TypeError, ValueError):
                    continue
            units.append((col, inv))

    if not units:
        # Direct DB path when fleet tree naming fails
        units = _units_from_db(db, tenant, inv_id=inv_id, array_id=array_id, array_name=array_name)
        if not units:
            return {
                "ok": True,
                "units": [],
                "note": (
                    "no matching inverters — pass inverter_id or array_name; "
                    "check tenant_census for ids"
                ),
            }

    out = []
    for col, inv in units[:40]:
        series, series_source = _daily_series_for_inv(db, inv, window_days, col=col)
        peers = [i for i in (col.get("inverters") or []) if _inv_pk(i) != _inv_pk(inv)]
        analysis = _trend_analysis(series, inv, peers, window_days)
        analysis.update({
            "inverter_id": _inv_pk(inv),
            "serial": inv.get("serial") or inv.get("sn"),
            "name": inv.get("name") or inv.get("sn") or inv.get("serial"),
            "array_name": _col_array_name(col),
            "array_id": col.get("array_id") or col.get("id"),
            "status_14d": inv.get("status"),
            "peer_index_14d": inv.get("peer_index"),
            "expected_low": bool(inv.get("expected_low")),
            "expected_low_reason": inv.get("expected_low_reason"),
            "expected_low_breach": bool(inv.get("expected_low_breach")),
            "series_source": series_source,
            "evidence_note": (
                "Only claim shading when pattern=steady_low AND days_of_history>=60 "
                "AND steady_underperformer_candidate=true. Otherwise list open hypotheses."
            ),
        })
        out.append(analysis)

    candidates = [u for u in out if u.get("steady_underperformer_candidate")]
    recent_drops = [u for u in out if u.get("pattern") == "recent_drop"]
    insufficient = [u for u in out if u.get("pattern") == "insufficient_history"]

    return {
        "ok": True,
        "window_days": window_days,
        "units": out,
        "steady_underperformer_candidates": candidates,
        "recent_drop_units": recent_drops,
        "insufficient_history_units": insufficient,
        "advice": (
            "steady_underperformer_candidate → ASK owner about shading/trees (do not assert); "
            "if they confirm, mark_inverter_expected_low. recent_drop → repair path. "
            "insufficient_history → do NOT invent shading; use production_history + "
            "query_tenant(inverter_daily|daily_generation|bills) for full record, or "
            "send tech to inspect."
        ),
    }


def _units_from_db(
    db,
    tenant: Tenant,
    *,
    inv_id=None,
    array_id=None,
    array_name: str | None = None,
) -> list[tuple[dict, dict]]:
    """ORM fallback when fleet-tree filter misses (name key bugs, etc.)."""
    q = select(Inverter).where(Inverter.tenant_id == tenant.id)
    if hasattr(Inverter, "deleted_at"):
        q = q.where(Inverter.deleted_at.is_(None))
    if inv_id is not None:
        try:
            q = q.where(Inverter.id == int(inv_id))
        except (TypeError, ValueError):
            return []
    if array_id is not None:
        try:
            q = q.where(Inverter.array_id == int(array_id))
        except (TypeError, ValueError):
            return []
    invs = db.execute(q.limit(80)).scalars().all()
    if array_name:
        names = {
            a.id: a.name
            for a in db.execute(
                select(Array).where(Array.tenant_id == tenant.id, Array.deleted_at.is_(None))
            ).scalars().all()
        }
        invs = [
            i for i in invs
            if array_name.lower() in (names.get(i.array_id) or "").lower()
        ]
    arr_ids = {i.array_id for i in invs if i.array_id}
    arr_map = {
        a.id: a
        for a in db.execute(select(Array).where(Array.id.in_(arr_ids or [-1]))).scalars().all()
    }
    # group for peer lists
    by_arr: dict[int, list] = {}
    for i in invs:
        by_arr.setdefault(i.array_id, []).append(i)
    out = []
    for i in invs:
        arr = arr_map.get(i.array_id)
        peers = [
            {
                "inverter_id": p.id,
                "id": p.id,
                "name": p.name or p.serial,
                "serial": p.serial,
                "nameplate_kw": getattr(p, "nameplate_kw", None),
            }
            for p in by_arr.get(i.array_id, [])
        ]
        col = {
            "array_id": i.array_id,
            "array_name": arr.name if arr else None,
            "inverters": peers,
        }
        inv = {
            "inverter_id": i.id,
            "id": i.id,
            "name": i.name or i.serial,
            "serial": i.serial,
            "nameplate_kw": getattr(i, "nameplate_kw", None),
        }
        out.append((col, inv))
    return out


def _daily_series_for_inv(
    db, inv: dict, window_days: int, *, col: dict | None = None
) -> tuple[list[dict], str]:
    """Prefer InverterDaily (all history in window); fall back to fleet-tree daily
    then array DailyGeneration (site total — marked as array_level)."""
    inv_id = _inv_pk(inv)
    since = (now().date() - timedelta(days=window_days))
    if inv_id:
        try:
            rows = db.execute(
                select(InverterDaily).where(
                    InverterDaily.inverter_id == int(inv_id),
                    InverterDaily.day >= since,
                ).order_by(InverterDaily.day.asc())
            ).scalars().all()
            if rows:
                return (
                    [
                        {
                            "date": r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day),
                            "kwh": float(r.kwh or 0),
                            "source": getattr(r, "source", None),
                        }
                        for r in rows
                    ],
                    "inverter_daily",
                )
        except Exception as e:
            log.info("inverter daily load failed: %s", e)

    daily = list(inv.get("daily") or [])
    if daily:
        series = [
            {"date": d.get("date"), "kwh": float(d.get("kwh") or 0)}
            for d in daily
            if d.get("date")
        ]
        if series:
            return series, "fleet_tree_daily"

    # Array-level DailyGeneration as last resort (site total, not per-inverter)
    aid = None
    if col:
        aid = col.get("array_id") or col.get("id")
    if aid is None and inv.get("array_id") is not None:
        aid = inv.get("array_id")
    if aid is not None:
        try:
            rows = db.execute(
                select(DailyGeneration).where(
                    DailyGeneration.array_id == int(aid),
                    DailyGeneration.day >= since,
                ).order_by(DailyGeneration.day.asc())
            ).scalars().all()
            if rows:
                return (
                    [
                        {
                            "date": r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day),
                            "kwh": float(r.kwh or 0),
                            "source": getattr(r, "source", None),
                        }
                        for r in rows
                    ],
                    "array_daily_generation",
                )
        except Exception as e:
            log.info("array daily_generation load failed: %s", e)
    return [], "none"


def _trend_analysis(series: list[dict], inv: dict, peers: list[dict], window_days: int) -> dict:
    """Compare early vs late halves of production; flag steady-low vs cliff."""
    if len(series) < 14:
        return {
            "pattern": "insufficient_history",
            "steady_underperformer_candidate": False,
            "timeline_summary": f"only {len(series)} daily points",
            "timeline": series[-30:],
        }

    kwhs = [float(p.get("kwh") or 0) for p in series]
    n = len(kwhs)
    # Split: first 60% vs last 40% (or first half vs second half for short series)
    split = max(7, int(n * 0.55))
    early = kwhs[:split]
    late = kwhs[split:]
    early_avg = sum(early) / len(early) if early else 0
    late_avg = sum(late) / len(late) if late else 0

    # Relative to own nameplate CF if available
    np_kw = float(inv.get("nameplate_kw") or 0) or None
    # Peer median of late window from fleet 14d if present
    peer_index = inv.get("peer_index")
    try:
        pi = float(peer_index) if peer_index is not None else None
    except (TypeError, ValueError):
        pi = None

    # Drop ratio
    if early_avg > 0.5:
        late_vs_early = late_avg / early_avg
    else:
        late_vs_early = 1.0 if late_avg < 1 else 2.0

    # Steady-low: consistently underproducing, little change over time
    under_14d = (inv.get("status") or "") == "underperforming" or (pi is not None and pi < 0.85)
    steady = (
        under_14d
        and late_vs_early >= 0.75  # not a recent cliff
        and early_avg > 0
        and late_avg > 0
        and n >= 60  # ~2 months of history preferred; soft with 30d
    )
    # Soften: if we only have ~30d but variance is low and peer is low
    if under_14d and not steady and n >= 21 and late_vs_early >= 0.85 and pi is not None and pi < 0.85:
        # Check coefficient of variation of weekly averages
        weeks = []
        for i in range(0, n, 7):
            chunk = kwhs[i:i + 7]
            if chunk:
                weeks.append(sum(chunk) / len(chunk))
        if len(weeks) >= 3:
            mean_w = sum(weeks) / len(weeks)
            if mean_w > 0:
                var = sum((w - mean_w) ** 2 for w in weeks) / len(weeks)
                cv = (var ** 0.5) / mean_w
                if cv < 0.35:
                    steady = True

    recent_drop = under_14d and late_vs_early < 0.55 and early_avg > 1.0

    if inv.get("expected_low"):
        pattern = "marked_expected_low"
        steady = False  # already handled
    elif recent_drop:
        pattern = "recent_drop"
    elif steady:
        pattern = "steady_low"
    elif under_14d:
        pattern = "underperforming_unclear"
    else:
        pattern = "ok"

    # Compact weekly timeline for the agent
    weekly = []
    for i in range(0, n, 7):
        chunk = series[i:i + 7]
        if not chunk:
            continue
        avg = sum(float(p.get("kwh") or 0) for p in chunk) / len(chunk)
        weekly.append({
            "week_start": chunk[0].get("date"),
            "avg_kwh_day": round(avg, 2),
        })

    return {
        "pattern": pattern,
        "steady_underperformer_candidate": bool(steady and not inv.get("expected_low")),
        "early_avg_kwh_day": round(early_avg, 2),
        "late_avg_kwh_day": round(late_avg, 2),
        "late_vs_early_ratio": round(late_vs_early, 3),
        "days_of_history": n,
        "timeline_weekly": weekly[-26:],  # ~6 months
        "timeline_summary": (
            f"{n}d history; early avg {early_avg:.1f} kWh/d → late {late_avg:.1f} kWh/d "
            f"({late_vs_early:.0%} of early); pattern={pattern}"
        ),
    }


# ── Full tenant history (vendor days + utility bills) ────────────────────────

def tenant_data_catalog(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Inventory of EVERY stored history stream for this tenant — what the agent
    can actually reason over (not just the last 14 days of fleet-tree)."""
    args = args or {}
    tid = tenant.id
    array_id = args.get("array_id")
    try:
        array_id = int(array_id) if array_id is not None else None
    except (TypeError, ValueError):
        array_id = None

    arr_q = select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
    if array_id is not None:
        arr_q = arr_q.where(Array.id == array_id)
    arrays = db.execute(arr_q).scalars().all()
    arr_ids = [a.id for a in arrays]

    # DailyGeneration depth
    dg = db.execute(
        select(
            func.min(DailyGeneration.day),
            func.max(DailyGeneration.day),
            func.count(DailyGeneration.id),
            func.coalesce(func.sum(DailyGeneration.kwh), 0.0),
        ).where(DailyGeneration.array_id.in_(arr_ids or [-1]))
    ).one()

    inv_q = select(Inverter).where(Inverter.tenant_id == tid)
    if hasattr(Inverter, "deleted_at"):
        inv_q = inv_q.where(Inverter.deleted_at.is_(None))
    if array_id is not None:
        inv_q = inv_q.where(Inverter.array_id == array_id)
    invs = db.execute(inv_q).scalars().all()
    inv_ids = [i.id for i in invs]

    idaily = db.execute(
        select(
            func.min(InverterDaily.day),
            func.max(InverterDaily.day),
            func.count(InverterDaily.id),
            func.coalesce(func.sum(InverterDaily.kwh), 0.0),
        ).where(InverterDaily.inverter_id.in_(inv_ids or [-1]))
    ).one()

    bills = db.execute(
        select(
            func.min(Bill.period_end),
            func.max(Bill.period_end),
            func.count(Bill.id),
            func.coalesce(func.sum(Bill.kwh_generated), 0.0),
            func.coalesce(func.sum(Bill.solar_credit_usd), 0.0),
        ).where(Bill.tenant_id == tid)
    ).one()

    ua_n = db.execute(
        select(func.count()).select_from(UtilityAccount).where(
            UtilityAccount.tenant_id == tid
        )
    ).scalar() or 0

    # Per-array coverage (compact)
    per_array = []
    for a in arrays[:80]:
        a_dg = db.execute(
            select(
                func.min(DailyGeneration.day),
                func.max(DailyGeneration.day),
                func.count(DailyGeneration.id),
            ).where(DailyGeneration.array_id == a.id)
        ).one()
        a_invs = [i for i in invs if i.array_id == a.id]
        a_iids = [i.id for i in a_invs]
        a_idaily = (None, None, 0)
        if a_iids:
            a_idaily = db.execute(
                select(
                    func.min(InverterDaily.day),
                    func.max(InverterDaily.day),
                    func.count(InverterDaily.id),
                ).where(InverterDaily.inverter_id.in_(a_iids))
            ).one()
        # bills linked via utility_accounts.array_id
        a_bills = db.execute(
            select(
                func.min(Bill.period_end),
                func.max(Bill.period_end),
                func.count(Bill.id),
            )
            .select_from(Bill)
            .join(UtilityAccount, UtilityAccount.id == Bill.account_id)
            .where(UtilityAccount.tenant_id == tid, UtilityAccount.array_id == a.id)
        ).one()
        per_array.append({
            "array_id": a.id,
            "array_name": a.name,
            "inverter_count": len(a_invs),
            "daily_generation": {
                "min": _dstr(a_dg[0]), "max": _dstr(a_dg[1]), "days": int(a_dg[2] or 0),
            },
            "inverter_daily": {
                "min": _dstr(a_idaily[0]), "max": _dstr(a_idaily[1]),
                "rows": int(a_idaily[2] or 0),
            },
            "bills_linked": {
                "min": _dstr(a_bills[0]), "max": _dstr(a_bills[1]),
                "count": int(a_bills[2] or 0),
            },
        })

    return {
        "ok": True,
        "tenant_id": tid,
        "streams": {
            "daily_generation": {
                "min": _dstr(dg[0]), "max": _dstr(dg[1]),
                "rows": int(dg[2] or 0), "total_kwh": round(float(dg[3] or 0), 1),
                "note": "Per-array vendor/utility daily kWh — primary long history",
            },
            "inverter_daily": {
                "min": _dstr(idaily[0]), "max": _dstr(idaily[1]),
                "rows": int(idaily[2] or 0), "total_kwh": round(float(idaily[3] or 0), 1),
                "note": "Per-inverter daily kWh — required for unit-level peer trends",
            },
            "bills": {
                "min": _dstr(bills[0]), "max": _dstr(bills[1]),
                "count": int(bills[2] or 0),
                "sum_kwh_generated": int(bills[3] or 0),
                "sum_solar_credit_usd": round(float(bills[4] or 0), 2),
                "utility_accounts": int(ua_n),
                "note": "Utility settlement periods — offtaker invoice source of truth",
            },
            "arrays": len(arrays),
            "inverters": len(invs),
        },
        "per_array": per_array,
        "how_to_read": (
            "Call production_history(array_name|inverter_id) for full series + peers + bills. "
            "query_tenant(resource=daily_generation|inverter_daily|bills, days=0) for raw rows. "
            "days=0 means ALL available history (no 90-day cap)."
        ),
    }


def _dstr(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10] if hasattr(v, "year") else v.isoformat()
    return str(v)[:32]


def production_history(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Full production + bill history for diagnosis — not the 14-day peer snapshot.

    Pulls ALL available InverterDaily / DailyGeneration in the window (default all
    history up to 10y), peer weekly series on the same array, and linked utility bills.
    Use this before claiming shading, soiling, string loss, or a real fault.
    """
    args = args or {}
    tid = tenant.id
    inv_id = args.get("inverter_id")
    array_id = args.get("array_id")
    array_name = (args.get("array_name") or "").strip()
    serial = (args.get("serial") or args.get("sn") or "").strip()
    try:
        window_days = int(args.get("window_days") if args.get("window_days") is not None else 0)
    except (TypeError, ValueError):
        window_days = 0
    # 0 = all history (cap 10 years for safety)
    if window_days <= 0:
        window_days = 3650
    window_days = max(14, min(window_days, 3650))
    since = now().date() - timedelta(days=window_days)

    # Resolve inverter / array
    inv_row = None
    if inv_id is not None:
        try:
            inv_row = db.get(Inverter, int(inv_id))
        except (TypeError, ValueError):
            inv_row = None
        if inv_row and inv_row.tenant_id != tid:
            inv_row = None
    if inv_row is None and serial:
        inv_row = db.execute(
            select(Inverter).where(
                Inverter.tenant_id == tid,
                Inverter.serial.ilike(f"%{serial}%"),
            ).limit(1)
        ).scalars().first()

    arr = None
    if array_id is not None:
        try:
            arr = db.get(Array, int(array_id))
        except (TypeError, ValueError):
            arr = None
        if arr and arr.tenant_id != tid:
            arr = None
    if arr is None and array_name:
        arr = db.execute(
            select(Array).where(
                Array.tenant_id == tid,
                Array.deleted_at.is_(None),
                Array.name.ilike(f"%{array_name}%"),
            ).limit(1)
        ).scalars().first()
    if arr is None and inv_row is not None:
        arr = db.get(Array, inv_row.array_id) if inv_row.array_id else None

    if arr is None and inv_row is None:
        return {
            "ok": False,
            "error": "pass inverter_id, serial, array_id, or array_name",
            "hint": "tenant_data_catalog lists every array and history depth",
        }

    # All inverters on array for peers
    peer_invs = []
    if arr is not None:
        peer_invs = list(db.execute(
            select(Inverter).where(
                Inverter.tenant_id == tid,
                Inverter.array_id == arr.id,
            )
        ).scalars().all())
        if hasattr(Inverter, "deleted_at"):
            peer_invs = [p for p in peer_invs if not getattr(p, "deleted_at", None)]

    if inv_row is None and peer_invs:
        # Site-level request — analyze array total + each inverter summary
        inv_row = None

    def _series_inv(iid: int) -> list[dict]:
        rows = db.execute(
            select(InverterDaily).where(
                InverterDaily.inverter_id == iid,
                InverterDaily.day >= since,
            ).order_by(InverterDaily.day.asc())
        ).scalars().all()
        return [
            {"date": r.day.isoformat(), "kwh": float(r.kwh or 0),
             "source": getattr(r, "source", None)}
            for r in rows
        ]

    def _series_array(aid: int) -> list[dict]:
        rows = db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == aid,
                DailyGeneration.day >= since,
            ).order_by(DailyGeneration.day.asc())
        ).scalars().all()
        return [
            {
                "date": r.day.isoformat(),
                "kwh": float(r.kwh or 0),
                "source": getattr(r, "source", None),
            }
            for r in rows
        ]

    def _weekly(series: list[dict]) -> list[dict]:
        weekly = []
        for i in range(0, len(series), 7):
            chunk = series[i:i + 7]
            if not chunk:
                continue
            avg = sum(float(p.get("kwh") or 0) for p in chunk) / len(chunk)
            weekly.append({
                "week_start": chunk[0].get("date"),
                "avg_kwh_day": round(avg, 2),
                "days": len(chunk),
                "total_kwh": round(sum(float(p.get("kwh") or 0) for p in chunk), 1),
            })
        return weekly

    def _monthly(series: list[dict]) -> list[dict]:
        buckets: dict[str, list[float]] = {}
        for p in series:
            d = str(p.get("date") or "")[:7]
            if len(d) < 7:
                continue
            buckets.setdefault(d, []).append(float(p.get("kwh") or 0))
        out = []
        for m in sorted(buckets.keys()):
            vals = buckets[m]
            out.append({
                "month": m,
                "days": len(vals),
                "total_kwh": round(sum(vals), 1),
                "avg_kwh_day": round(sum(vals) / len(vals), 2) if vals else 0,
            })
        return out

    # Subject series
    subject = {}
    if inv_row is not None:
        s = _series_inv(inv_row.id)
        src = "inverter_daily"
        if not s and arr is not None:
            s = _series_array(arr.id)
            src = "array_daily_generation_fallback"
        analysis = _trend_analysis(
            s,
            {
                "inverter_id": inv_row.id,
                "nameplate_kw": getattr(inv_row, "nameplate_kw", None),
                "status": None,
                "peer_index": None,
            },
            [],
            window_days,
        )
        subject = {
            "kind": "inverter",
            "inverter_id": inv_row.id,
            "serial": inv_row.serial,
            "name": inv_row.name or inv_row.serial,
            "nameplate_kw": getattr(inv_row, "nameplate_kw", None),
            "vendor": inv_row.vendor,
            "series_source": src,
            "days_of_history": len(s),
            "first_day": s[0]["date"] if s else None,
            "last_day": s[-1]["date"] if s else None,
            "total_kwh": round(sum(float(p["kwh"]) for p in s), 1),
            "weekly": _weekly(s)[-104:],  # ~2y
            "monthly": _monthly(s),
            "trend": analysis,
            # raw daily only if short enough for the model
            "daily": s if len(s) <= 120 else None,
            "daily_omitted_count": 0 if len(s) <= 120 else len(s),
        }
    elif arr is not None:
        s = _series_array(arr.id)
        analysis = _trend_analysis(
            s, {"status": None, "peer_index": None}, [], window_days,
        )
        subject = {
            "kind": "array",
            "array_id": arr.id,
            "array_name": arr.name,
            "series_source": "daily_generation",
            "days_of_history": len(s),
            "first_day": s[0]["date"] if s else None,
            "last_day": s[-1]["date"] if s else None,
            "total_kwh": round(sum(float(p["kwh"]) for p in s), 1),
            "weekly": _weekly(s)[-104:],
            "monthly": _monthly(s),
            "trend": analysis,
            "daily": s if len(s) <= 120 else None,
            "daily_omitted_count": 0 if len(s) <= 120 else len(s),
        }

    # Peer inverter summaries on same array
    peers_out = []
    for p in peer_invs:
        if inv_row is not None and p.id == inv_row.id:
            continue
        ps = _series_inv(p.id)
        if not ps:
            continue
        peers_out.append({
            "inverter_id": p.id,
            "serial": p.serial,
            "name": p.name or p.serial,
            "nameplate_kw": getattr(p, "nameplate_kw", None),
            "days_of_history": len(ps),
            "total_kwh": round(sum(float(x["kwh"]) for x in ps), 1),
            "avg_kwh_day": round(
                sum(float(x["kwh"]) for x in ps) / len(ps), 2
            ) if ps else 0,
            "monthly": _monthly(ps)[-24:],
            "weekly_tail": _weekly(ps)[-12:],
        })

    # Peer ratio: subject avg / peer mean avg (when both have data)
    peer_ratio = None
    if subject.get("days_of_history") and peers_out:
        sub_avg = subject["total_kwh"] / max(subject["days_of_history"], 1)
        peer_avgs = [p["avg_kwh_day"] for p in peers_out if p["avg_kwh_day"] > 0]
        if peer_avgs and sub_avg > 0:
            peer_mean = sum(peer_avgs) / len(peer_avgs)
            if peer_mean > 0:
                peer_ratio = round(sub_avg / peer_mean, 3)

    # Utility bills for this array (and tenant-wide sample if none linked)
    bill_rows = []
    if arr is not None:
        bq = (
            select(Bill, UtilityAccount)
            .join(UtilityAccount, UtilityAccount.id == Bill.account_id)
            .where(
                Bill.tenant_id == tid,
                UtilityAccount.array_id == arr.id,
            )
            .order_by(Bill.period_end.desc().nulls_last())
            .limit(120)
        )
        for b, ua in db.execute(bq).all():
            bill_rows.append(_bill_row(b, ua))
    if not bill_rows:
        # Tenant bills (newest 60) — agent still sees settlement history
        bq = (
            select(Bill, UtilityAccount)
            .join(UtilityAccount, UtilityAccount.id == Bill.account_id)
            .where(Bill.tenant_id == tid)
            .order_by(Bill.period_end.desc().nulls_last())
            .limit(60)
        )
        for b, ua in db.execute(bq).all():
            bill_rows.append(_bill_row(b, ua))
        bill_scope = "tenant_all"
    else:
        bill_scope = "array_linked"

    # Honest judgment scaffolding for the model
    trend_pat = (subject.get("trend") or {}).get("pattern")
    days_h = int(subject.get("days_of_history") or 0)
    if days_h < 21:
        judgment = (
            "INSUFFICIENT_HISTORY — do not claim shading or permanent defect. "
            "State peer_index if any, list open hypotheses, offer portal check or tech visit."
        )
    elif trend_pat == "recent_drop":
        judgment = (
            "RECENT_DROP pattern in stored kWh — treat as possible fault/soiling/outage, "
            "not long-term shading."
        )
    elif trend_pat == "steady_low" and days_h >= 60:
        judgment = (
            "STEADY_LOW with multi-month history — shading/orientation is a *candidate* only. "
            "Ask the owner; do not assert. Compare peers monthly below."
        )
    elif trend_pat == "underperforming_unclear":
        judgment = (
            "UNDERPERFORMING but pattern unclear — need owner context or more series. "
            "Do not lead with shading."
        )
    else:
        judgment = f"pattern={trend_pat}; days={days_h}; report numbers, not a single root cause."

    return {
        "ok": True,
        "window_days": window_days,
        "since": since.isoformat(),
        "array": (
            {"id": arr.id, "name": arr.name} if arr is not None else None
        ),
        "subject": subject,
        "peers_on_array": peers_out,
        "subject_vs_peer_avg_ratio": peer_ratio,
        "bills": {
            "scope": bill_scope,
            "count": len(bill_rows),
            "rows": bill_rows,
        },
        "judgment_guardrail": judgment,
        "evidence_rules": [
            "Never say 'it is shading' — only 'steady-low pattern consistent with permanent "
            "shade IF owner confirms a physical cause'.",
            "14-day peer_index alone is not multi-year proof.",
            "Utility bills settle site totals (not per-inverter) — use them for array health "
            "and offtaker $, not unit #2 alone.",
            "If inverter_daily is empty, say so and use array daily_generation + peers carefully.",
        ],
    }


def _bill_row(b: Bill, ua: UtilityAccount | None = None) -> dict:
    solar = getattr(b, "solar_credit_usd", None) or getattr(b, "net_credit", None)
    excess = getattr(b, "kwh_sent_to_grid", None)
    implied = None
    try:
        if solar is not None and excess and float(excess) > 0:
            implied = round(float(solar) / float(excess), 6)
    except Exception:
        pass
    return {
        "id": b.id,
        "utility_account_id": getattr(b, "account_id", None),
        "array_id": getattr(ua, "array_id", None) if ua is not None else None,
        "provider": getattr(ua, "provider", None) if ua is not None else None,
        "account_nickname": getattr(ua, "nickname", None) if ua is not None else None,
        "period_start": _dstr(getattr(b, "period_start", None)),
        "period_end": _dstr(getattr(b, "period_end", None)),
        "kwh_generated": getattr(b, "kwh_generated", None),
        "kwh_consumed": getattr(b, "kwh_consumed", None),
        "kwh_sent_to_grid": excess,
        "solar_credit_usd": solar,
        "implied_rate_per_kwh": implied,
        "total_cost": getattr(b, "total_cost", None),
        "document_number": getattr(b, "document_number", None),
    }


# ── save_document ────────────────────────────────────────────────────────────

def _docs_dir(tenant_id: str) -> Path:
    root = Path(os.getenv("STORAGE_DIR", "storage"))
    d = root / "agent_docs" / tenant_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_document(db, tenant: Tenant, *, args: dict) -> dict:
    """Persist a chat-produced artifact; optionally render PDF and attach to ticket."""
    content = (args.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}
    title = (args.get("title") or "").strip() or "Energy Agent document"
    doc_type = (args.get("type") or args.get("doc_type") or "note").strip().lower()
    if doc_type not in _DOC_TYPES:
        doc_type = "other"
    ticket_id = args.get("ticket_id")
    make_pdf = args.get("make_pdf", True)
    content_format = (args.get("format") or "markdown").strip().lower()
    if content_format not in ("markdown", "text", "html"):
        content_format = "markdown"

    if ticket_id is not None:
        t = db.get(RepairTicket, int(ticket_id))
        if t is None or t.tenant_id != tenant.id:
            return {"ok": False, "error": "ticket not found"}

    doc = AgentDocument(
        tenant_id=tenant.id,
        ticket_id=int(ticket_id) if ticket_id is not None else None,
        doc_type=doc_type,
        title=title[:240],
        content=content[:200_000],
        content_format=content_format,
        created_by="agent",
    )
    db.add(doc)
    db.flush()

    pdf_path = None
    pdf_error = None
    if make_pdf:
        try:
            pdf_path = _render_doc_pdf(tenant, doc)
            doc.pdf_path = pdf_path
        except Exception as e:
            pdf_error = str(e)[:200]
            log.warning("save_document pdf failed: %s", e)

    db.flush()
    return {
        "ok": True,
        "document": {
            "id": doc.id,
            "ticket_id": doc.ticket_id,
            "type": doc.doc_type,
            "title": doc.title,
            "content_format": doc.content_format,
            "content_chars": len(doc.content or ""),
            "pdf_path": doc.pdf_path,
            "pdf_error": pdf_error,
            "created_at": _iso(doc.created_at),
            "download_hint": (
                f"/v1/energy-agent/documents/{doc.id}" if doc.id else None
            ),
        },
        "message": (
            f"Saved “{title}” as document #{doc.id}"
            + (f" on ticket #{ticket_id}" if ticket_id else "")
            + (" (PDF ready)" if doc.pdf_path else (" (text only — PDF failed)" if pdf_error else ""))
        ),
    }


def _render_doc_pdf(tenant: Tenant, doc: AgentDocument) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted
    from reportlab.lib.enums import TA_LEFT

    out_dir = _docs_dir(tenant.id)
    fname = f"doc_{doc.id}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', doc.title)[:40]}.pdf"
    path = out_dir / fname

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle", parent=styles["Heading1"], fontSize=14, spaceAfter=8,
    )
    meta_style = ParagraphStyle(
        "DocMeta", parent=styles["Normal"], fontSize=9, textColor="#555555", spaceAfter=12,
    )
    body_style = ParagraphStyle(
        "DocBody", parent=styles["Normal"], fontSize=10, leading=14, alignment=TA_LEFT,
    )

    story = []
    story.append(Paragraph(_esc(doc.title), title_style))
    meta = (
        f"Type: {doc.doc_type} · Tenant: {getattr(tenant, 'company_name', None) or tenant.name or tenant.id}"
        + (f" · Ticket #{doc.ticket_id}" if doc.ticket_id else "")
        + f" · {now().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    story.append(Paragraph(_esc(meta), meta_style))
    story.append(Spacer(1, 0.15 * inch))

    # Simple markdown-ish → paragraphs
    text = doc.content or ""
    if doc.content_format == "html":
        text = re.sub(r"<[^>]+>", " ", text)
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            block = block.lstrip("#").strip()
            story.append(Paragraph(_esc(block), styles["Heading2"]))
        elif block.startswith("```"):
            inner = re.sub(r"^```\w*\n?", "", block)
            inner = re.sub(r"\n?```$", "", inner)
            story.append(Preformatted(inner[:8000], styles["Code"]))
        else:
            # collapse single newlines within para
            para = " ".join(line.strip() for line in block.splitlines())
            story.append(Paragraph(_esc(para), body_style))
        story.append(Spacer(1, 0.08 * inch))

    doc_pdf = SimpleDocTemplate(
        str(path), pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    doc_pdf.build(story)
    return str(path)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def get_document(db, tenant: Tenant, doc_id: int) -> dict:
    doc = db.get(AgentDocument, int(doc_id))
    if doc is None or doc.tenant_id != tenant.id:
        return {"ok": False, "error": "document not found"}
    return {
        "ok": True,
        "document": {
            "id": doc.id,
            "ticket_id": doc.ticket_id,
            "type": doc.doc_type,
            "title": doc.title,
            "content": doc.content,
            "content_format": doc.content_format,
            "pdf_path": doc.pdf_path,
            "created_at": _iso(doc.created_at),
        },
    }


def list_documents(db, tenant: Tenant, *, ticket_id: int | None = None, limit: int = 20) -> dict:
    q = select(AgentDocument).where(AgentDocument.tenant_id == tenant.id)
    if ticket_id is not None:
        q = q.where(AgentDocument.ticket_id == int(ticket_id))
    q = q.order_by(desc(AgentDocument.created_at)).limit(max(1, min(50, limit)))
    rows = list(db.execute(q).scalars().all())
    return {
        "ok": True,
        "documents": [
            {
                "id": d.id,
                "ticket_id": d.ticket_id,
                "type": d.doc_type,
                "title": d.title,
                "pdf_path": d.pdf_path,
                "created_at": _iso(d.created_at),
                "content_chars": len(d.content or ""),
            }
            for d in rows
        ],
        "count": len(rows),
    }
