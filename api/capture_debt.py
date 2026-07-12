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
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text

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
# when the captured token is older than this. Also the bar for a co-op's browser-
# fed GENERATION stream going stale (see _stale_coop_providers).
UTILITY_STALE_DAYS = 5

# Co-op generation only ever arrives via these (client-side, browser-captured)
# sources. There is deliberately NO server-side SmartHub pull — the NISC usage
# API is cookie-bound and can't be replayed headlessly (see the unscheduled
# api/jobs/smarthub_pull.py). 'smarthub' is listed only for forward-compat; it
# has never produced a row.
_COOP_GEN_SOURCES = ("utility_meter", "smarthub")


def _stale_coop_providers(db, tenant_id: str, today: date | None = None) -> dict[str, dict]:
    """SmartHub providers whose browser-fed GENERATION stream has gone stale.

    Keyed on the DATA stream (newest DailyGeneration day on any array linked to
    an enabled SmartHub account), NOT on utility_sessions — because SmartHub
    meter-captures never carry an apiToken, so they never refresh a session row,
    so a real co-op customer with fresh browser data but no stored session is
    INVISIBLE to the session-based loop (prod 2026-07-02: ten_bae078ae4c81bb24,
    VEC West Glover, had current generation but drain_utilities=[]). Flagging by
    the data stream instead covers every co-op, session or not, so any waking
    browser drains it via the extension's recaptureVendor(vec/wec) portal path.
    SQLAlchemy Core so it runs on both Postgres and SQLite.

    Returns {provider: {"reason": "gen_stale_Nd", "last_day": "YYYY-MM-DD"}}.
    """
    from .adapters.smarthub import is_smarthub_provider
    from .models import DailyGeneration, UtilityAccount

    today = today or datetime.utcnow().date()
    rows = db.execute(
        select(UtilityAccount.provider, func.max(DailyGeneration.day))
        .join(
            DailyGeneration,
            (DailyGeneration.array_id == UtilityAccount.array_id)
            & (DailyGeneration.tenant_id == UtilityAccount.tenant_id),
        )
        .where(
            UtilityAccount.tenant_id == tenant_id,
            UtilityAccount.array_id.is_not(None),
            UtilityAccount.enabled.is_(True),
            UtilityAccount.deleted_at.is_(None),
            DailyGeneration.source.in_(_COOP_GEN_SOURCES),
        )
        .group_by(UtilityAccount.provider)
    ).all()
    out: dict[str, dict] = {}
    for provider, last_day in rows:
        if not is_smarthub_provider(provider or ""):
            continue
        if last_day is None:
            continue          # never captured → onboarding's problem, not debt
        behind = (today - last_day).days
        if behind >= UTILITY_STALE_DAYS:
            out[provider] = {"reason": "gen_stale_%dd" % behind,
                             "last_day": last_day.isoformat()}
    return out


def cloud_capture_providers(db, tenant_id: str) -> list[str]:
    """Providers this tenant has ACTIVATED for server-side Cloud Capture (password
    stored in PortalCredential + opted in). Once a login lives here, the harvester
    farm refreshes it around the clock, so the extension must STAND DOWN for that
    provider: no capture-debt drains (excluded below) AND no Chrome 'reconnect'
    nudges (Ford 2026-07-11 — a customer who handed us their password server-side
    should never also get the extension nagging them to sign in). Recovery for a
    failing server-side login lives in the app's Credential Vault, not a
    notification. Never raises — this rides the sacred heartbeat.
    """
    from .models import PortalCredential
    try:
        rows = db.execute(
            select(PortalCredential.provider)
            .where(
                PortalCredential.tenant_id == tenant_id,
                PortalCredential.cloud_capture_enabled.is_(True),
                PortalCredential.secret_enc.is_not(None),
            )
            .distinct()
        ).all()
        return sorted({(p or "").strip().lower() for (p,) in rows if p})
    except Exception:                                    # noqa: BLE001
        log.exception("cloud_capture_providers failed for %s", tenant_id)
        return []


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
       "utilities": {"gmp": {"reason": "expires_in_2d"},
                     "vec": {"reason": "gen_stale_6d", "last_day": "2026-06-27"}},
       "drain": ["fronius"], "drain_chint": true, "keepalive_gmp": true,
       "drain_utilities": ["vec"]}
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

    # Stale co-op GENERATION streams — the signal that actually matters for
    # SmartHub (there is no server-side pull; the browser is the only source).
    # This catches co-op customers the session loop above misses entirely
    # (fresh browser data, no stored session). A data-driven reason wins over a
    # bare token-age one. Any waking browser then drains them via drain_utilities.
    for provider, info in _stale_coop_providers(db, tenant_id, today).items():
        debt_utils[provider] = info

    # Tight self-heal (Ford, 2026-07-08: "all accounts should be babied like
    # this") — a MUCH tighter (minutes, not days) staleness bar merged in here
    # so the SAME drain mechanism below fires on the tenant's very next
    # heartbeat, instead of waiting for VENDOR_STALE_DAYS to pass. Universal:
    # every tenant gets this, not just vip_watch ones (see api/vip_watch.py —
    # vip_watch now only controls the FASTER tier of the separate Ford-alert
    # sweep, since alerting Ford is the one half that can't scale to everyone
    # without flooding his inbox).
    from .vip_watch import vip_stale_vendors
    for v in vip_stale_vendors(db, tenant_id):
        debt_vendors.setdefault(v, {"reason": "stale_self_heal"})

    # Server-side Cloud Capture owns these providers — the harvester farm refreshes
    # them 24/7, so never ask the extension to open a tab / auto-login for one (that
    # would double-capture and, worse, risk tripping the very "suspicious sign-in"
    # alerts the server-side path is built to avoid). Drop them from the debt.
    cc = set(cloud_capture_providers(db, tenant_id))
    if cc:
        for v in [v for v in debt_vendors if v in cc]:
            debt_vendors.pop(v, None)
        for p in [p for p in debt_utils if p in cc]:
            debt_utils.pop(p, None)

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
