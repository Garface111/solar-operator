"""Data-freshness watchdog: alert when an active GMP tenant has stopped capturing.

GMP data only refreshes when the EnergyAgent extension runs in the owner's logged-in
browser — there is no server-side refresh (GMP blocks it). So an account silently
goes stale the moment the extension stops (browser closed for days, session expired,
extension removed). We then keep billing offtakers and rendering daily reports from
FROZEN data with nobody noticing — the exact silent-trust failure we keep hitting.

This watchdog measures, per active tenant with GMP accounts, the most recent capture
(latest Bill.pulled_at OR latest GMP DailyGeneration.uploaded_at) and alerts once if
any tenant has gone quiet for >= STALE_DAYS. Read-only: it ALERTS, never mutates.
The scan function also powers an on-demand freshness read for the UI/API.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from datetime import timedelta

from ..db import SessionLocal
from ..models import Tenant, UtilityAccount, Array, Bill, DailyGeneration, InverterAlertState
from ..notify import send_internal_alert

log = logging.getLogger(__name__)

STALE_DAYS = 4
# Re-alert a still-stale tenant at most this often (matches the sibling
# gmp_final_expiry_warnings / coop_session_death_warnings cadence). This
# per-tenant dedup is what lets the scan run DAILY instead of weekly without
# spamming Ford -- the weekly cadence was a false economy that let a frozen GMP
# capture (the extension stopped, token still valid) go unseen up to ~14 days
# while invoices generated from stale data (Ford, 2026-07-09: no scarcity-driven
# self-sabotage; a slow watchdog on the billing-critical path is exactly that).
_REALERT_DAYS = 3
# DailyGeneration sources that represent a REAL GMP capture (not the bill_prorate
# estimate, not a vendor inverter pull).
_GMP_DAILY_SOURCES = ("utility_meter", "gmp_api", "smarthub")


def tenant_gmp_freshness(db, tenant_id: str) -> dict | None:
    """Most-recent GMP capture for one tenant, or None if it has no GMP accounts.
    Returns {tenant_id, accounts, last_capture (datetime|None), days_stale (int|None)}."""
    accts = [a for (a,) in db.execute(select(UtilityAccount.id).where(
        UtilityAccount.tenant_id == tenant_id,
        UtilityAccount.provider == "gmp")).all()]
    if not accts:
        return None
    arr_ids = [a for (a,) in db.execute(select(Array.id).where(
        Array.tenant_id == tenant_id)).all()]
    last_bill = db.execute(select(func.max(Bill.pulled_at))
                           .where(Bill.account_id.in_(accts))).scalar()
    last_daily = (db.execute(select(func.max(DailyGeneration.uploaded_at)).where(
        DailyGeneration.array_id.in_(arr_ids),
        DailyGeneration.source.in_(_GMP_DAILY_SOURCES))).scalar()
        if arr_ids else None)
    candidates = [d for d in (last_bill, last_daily) if d is not None]
    last = max(candidates) if candidates else None
    days = (datetime.utcnow() - last).days if last is not None else None
    return {"tenant_id": tenant_id, "accounts": len(accts),
            "last_capture": last, "days_stale": days}


def scan_stale_gmp_captures(stale_days: int = STALE_DAYS) -> dict:
    """Return {'stale': [...], 'checked': int, 'ok': bool}. Pure read — never
    mutates. A tenant is stale when its newest GMP capture is >= stale_days old
    (or it has GMP accounts but no capture at all)."""
    stale: list[dict] = []
    checked = 0
    with SessionLocal() as db:
        tids = [t for (t,) in db.execute(select(UtilityAccount.tenant_id)
                .where(UtilityAccount.provider == "gmp").distinct()).all()]
        for tid in tids:
            t = db.get(Tenant, tid)
            if not t or not t.active:
                continue
            f = tenant_gmp_freshness(db, tid)
            if f is None:
                continue
            checked += 1
            d = f["days_stale"]
            if d is None or d >= stale_days:
                stale.append({
                    "tenant_id": tid,
                    "tenant": getattr(t, "contact_email", None) or tid,
                    "product": t.product,
                    "accounts": f["accounts"],
                    "last_capture": f["last_capture"].isoformat() if f["last_capture"] else None,
                    "days_stale": d,
                })
    return {"stale": stale, "checked": checked, "ok": not stale}


def run_gmp_freshness_watchdog(stale_days: int = STALE_DAYS, dry_run: bool = False) -> dict:
    """Alert if any active GMP tenant has gone quiet for >= stale_days. Safe to run
    DAILY: per-tenant InverterAlertState dedup ('gmp_freshness:<tenant>') means a
    still-stale tenant re-alerts at most every _REALERT_DAYS, a recovered tenant's
    incident clears, and one aggregate email covers only the tenants that are newly
    stale or past their re-alert window. Stays SILENT when nothing is newly
    actionable. Returns the scan result plus {'alerted','cleared'}."""
    result = scan_stale_gmp_captures(stale_days)
    now_ = datetime.utcnow()
    realert_cutoff = now_ - timedelta(days=_REALERT_DAYS)
    stale_by_tid = {s["tenant_id"]: s for s in result["stale"]}
    to_alert: list[dict] = []
    cleared = 0

    with SessionLocal() as db:
        # Clear incidents for tenants that RECOVERED (have an open incident but are
        # no longer in the stale set) so the next freeze is a fresh alert.
        open_states = db.execute(select(InverterAlertState).where(
            InverterAlertState.incident_key.startswith("gmp_freshness:"))).scalars().all()
        for st in open_states:
            tid = st.incident_key.split("gmp_freshness:", 1)[1]
            if tid not in stale_by_tid:
                if not dry_run:
                    db.delete(st)
                cleared += 1
        if not dry_run:
            db.commit()

        # Decide who to alert: newly stale (no open incident) or past the re-alert bar.
        for tid, s in stale_by_tid.items():
            key = f"gmp_freshness:{tid}"
            st = db.execute(select(InverterAlertState).where(
                InverterAlertState.incident_key == key)).scalar_one_or_none()
            if st is not None and st.last_alerted_at is not None and st.last_alerted_at >= realert_cutoff:
                continue                       # already alerted recently -> dedup
            to_alert.append(s)
            if not dry_run:
                if st is None:
                    st = InverterAlertState(tenant_id=tid, incident_key=key)
                    db.add(st)
                st.last_alerted_at = now_
        if not dry_run:
            db.commit()

    result["cleared"] = cleared
    result["alerted"] = [s["tenant_id"] for s in to_alert]

    if not to_alert:
        log.info("gmp_freshness_watchdog: %d checked, %d stale, nothing new to alert "
                 "(cleared %d recovered)", result["checked"], len(result["stale"]), cleared)
        return result

    stale = sorted(to_alert,
                   key=lambda x: (x["days_stale"] is None, x["days_stale"] or 0),
                   reverse=True)
    lines = [
        f"⚠️ {len(stale)} active GMP tenant(s) have not captured in ≥{stale_days} days "
        f"({result['checked']} checked).",
        "GMP data refreshes ONLY when the extension runs in the owner's logged-in "
        "browser (no server-side refresh). A stale tenant means offtaker invoices "
        "and daily reports are being built from frozen data — check the extension / "
        "GMP login for these owners.",
        "",
    ]
    for s in stale[:40]:
        d = "never captured" if s["days_stale"] is None else f"{s['days_stale']}d ago"
        lines.append(f"  {s['tenant']} [{s['product']}] {s['accounts']} accts: "
                     f"last capture {s['last_capture'] or '—'} ({d})")
    if len(stale) > 40:
        lines.append(f"  … and {len(stale) - 40} more.")

    if not dry_run:
        send_internal_alert(
            f"GMP freshness: {len(stale)} stale tenant(s)", "\n".join(lines))
    log.warning("gmp_freshness_watchdog: alerted %d stale GMP tenants (cleared %d)",
                len(stale), cleared)
    return result
