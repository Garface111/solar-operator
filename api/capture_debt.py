"""Capture debt — the server-side answer to the single-machine problem.

The extension vendors (Fronius / SMA / Chint) and the utility portals only
capture while SOME browser with the extension is open. When the operator's
machine sleeps, data silently stops. This module makes the SERVER the
authority on what's owed: each extension heartbeat (every 60s from every
signed-in browser) gets back a small "debt" object listing exactly which
vendors/utilities are stale, and the extension drains it through its existing
capture machinery. Any machine that wakes first pays the debt — so a second
laptop (or Bruce's desktop) is automatic redundancy, no coordination needed
(captures are idempotent server-side).

SolarEdge is excluded: it's pulled server-side every 5 minutes regardless of
any browser (poll_inverter_sources).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

log = logging.getLogger("capture_debt")

# A vendor is IN DEBT when its newest daily row is older than this. Yesterday's
# row landing today is normal (vendors post day-totals with lag), so the bar is
# "we don't even have the day before yesterday".
VENDOR_STALE_DAYS = 2
# Extension-captured vendors only. SolarEdge = server-side, never in debt here.
EXTENSION_VENDORS = ("fronius", "sma", "chint")
# GMP: ask for a keepalive visit when the JWT is inside this window (mirrors
# the extension's own GMP_RECAP_AHEAD_DAYS so either trigger works).
GMP_KEEPALIVE_AHEAD_DAYS = 8
# Co-op (SmartHub) sessions have no tracked expiry — nudge a portal re-visit
# when the captured token is older than this.
UTILITY_STALE_DAYS = 5

# Best-effort duplicate-drain damper: once debt is HANDED to some browser,
# don't hand the same tenant's debt out again for this long. In-process only
# (single web instance today); a second instance would merely double-issue an
# idempotent sweep, which is harmless.
DRAIN_COOLDOWN_MIN = 45
_last_issued: dict[str, datetime] = {}


def compute_capture_debt(db, tenant_id: str) -> dict | None:
    """The tenant's outstanding capture debt, or None when all current.

    Shape (only stale entries appear):
      {"vendors":   {"fronius": {"last_day": "2026-06-29", "days_behind": 3}},
       "utilities": {"gmp": {"reason": "expires_in_2d"}},
       "drain": ["fronius"], "drain_chint": true, "keepalive_gmp": true}
    """
    debt_vendors: dict[str, dict] = {}
    debt_utils: dict[str, dict] = {}

    rows = db.execute(text("""
        SELECT i.vendor, max(d.day) AS last_day
        FROM inverters i
        LEFT JOIN inverter_daily d ON d.inverter_id = i.id
        WHERE i.tenant_id = :t AND i.vendor = ANY(:vv)
        GROUP BY i.vendor"""), {"t": tenant_id, "vv": list(EXTENSION_VENDORS)})
    today = datetime.utcnow().date()
    for vendor, last_day in rows:
        if last_day is None:
            continue          # never captured → onboarding's problem, not debt
        behind = (today - last_day).days
        if behind >= VENDOR_STALE_DAYS:
            debt_vendors[vendor] = {"last_day": last_day.isoformat(),
                                    "days_behind": behind}

    for provider, captured_at, expires_at in db.execute(text("""
        SELECT provider, max(captured_at), max(expires_at)
        FROM utility_sessions WHERE tenant_id = :t GROUP BY provider"""),
            {"t": tenant_id}):
        now = datetime.utcnow()
        if provider == "gmp":
            if expires_at is not None and \
                    expires_at - now < timedelta(days=GMP_KEEPALIVE_AHEAD_DAYS):
                days = max(0.0, (expires_at - now).total_seconds() / 86400)
                debt_utils["gmp"] = {"reason": "expires_in_%.1fd" % days}
        elif captured_at is not None and \
                now - captured_at > timedelta(days=UTILITY_STALE_DAYS):
            days = (now - captured_at).days
            debt_utils[provider] = {"reason": "token_age_%dd" % days}

    if not debt_vendors and not debt_utils:
        return None
    return {
        "vendors": debt_vendors,
        "utilities": debt_utils,
        # What the extension should actually DO, pre-chewed so background.js
        # stays dumb: parallel background sweep for the portal vendors, the
        # route-walk recapture for Chint, a keepalive visit for GMP, and a
        # portal re-visit for stale co-ops.
        "drain": sorted(v for v in debt_vendors if v in ("fronius", "sma")),
        "drain_chint": "chint" in debt_vendors,
        "keepalive_gmp": "gmp" in debt_utils,
        "drain_utilities": sorted(p for p in debt_utils if p != "gmp"),
    }


def debt_for_heartbeat(db, tenant_id: str) -> dict | None:
    """compute_capture_debt guarded by the duplicate-drain damper. Called on
    every extension heartbeat; must never raise (heartbeat is sacred)."""
    try:
        last = _last_issued.get(tenant_id)
        if last and datetime.utcnow() - last < timedelta(minutes=DRAIN_COOLDOWN_MIN):
            return None
        debt = compute_capture_debt(db, tenant_id)
        if debt:
            _last_issued[tenant_id] = datetime.utcnow()
        return debt
    except Exception:                                    # noqa: BLE001
        log.exception("capture debt computation failed for %s", tenant_id)
        return None
