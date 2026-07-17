"""Portal access roster — the dashboard's "master account" view (v1.9.112).

Answers the multi-client operator's question: FOR EACH CLIENT, is their utility
portal login saved in my extension vault (fully hands-off), failing (password
changed), or still to be collected?

Two halves:
  * ingest_vault_report() — called by the /v1/extension/heartbeat handler when a
    ping carries a vault report. Persists login METADATA (provider + username +
    health) to PortalLoginStatus. Passwords never reach the server by design —
    see extension/vault.js "SECURITY POSTURE".
  * GET /v1/portal-access — session-authed roster for the dashboard tab. Joins
    each active Client's portal identity (Client.gmp_email/gmp_username, and the
    shared vec_* pair for SmartHub co-ops) against the reported logins.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Header
from sqlalchemy import select

from .db import SessionLocal
from .models import Client, PortalLoginStatus, Tenant, now

router = APIRouter()

# Auto-login pauses after this many failed attempts (extension/background.js
# AUTOLOGIN_MAX_VENDOR_FAILS) — mirrored here for the roster's "failing" state.
_PAUSE_FAILS = 3
# A login whose last successful pull is older than this is "stale" — the
# background refresh runs ~every 12h, so 48h means two+ missed cycles.
_STALE_AFTER = timedelta(hours=48)
# The extension heartbeats every 60s; 5 minutes of silence = not running.
_EXT_ALIVE_WINDOW = timedelta(minutes=5)


def _parse_iso(v) -> Optional[datetime]:
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def ingest_vault_report(db, tenant_id: str, report: list) -> None:
    """Upsert the extension's vault snapshot into PortalLoginStatus.

    Per-provider REPLACE: the report is a full snapshot of what the vault holds,
    so usernames absent from it are deleted for the providers it covers — a
    login the operator removed in the popup disappears from the roster instead
    of lingering as "saved" forever. Providers NOT in the report are untouched
    (an empty report list is a no-op, never a wipe).
    """
    seen: dict[str, set[str]] = {}
    for e in report[:100]:                       # bound a hostile/buggy payload
        if not isinstance(e, dict):
            continue
        provider = str(e.get("code") or "").strip().lower()[:40]
        username = str(e.get("username") or "").strip()[:200]
        if not provider or not username:
            continue
        username_lc = username.lower()
        seen.setdefault(provider, set()).add(username_lc)
        row = db.execute(
            select(PortalLoginStatus).where(
                PortalLoginStatus.tenant_id == tenant_id,
                PortalLoginStatus.provider == provider,
                PortalLoginStatus.username_lc == username_lc,
            )
        ).scalar_one_or_none()
        if row is None:
            row = PortalLoginStatus(
                tenant_id=tenant_id, provider=provider,
                username=username, username_lc=username_lc,
            )
            db.add(row)
        was_failing = bool(row.paused) or (row.fails or 0) >= _PAUSE_FAILS
        row.username = username
        row.enabled = bool(e.get("enabled", True))
        row.paused = bool(e.get("paused", False))
        try:
            row.fails = max(0, min(int(e.get("fails") or 0), 999))
        except Exception:
            row.fails = 0
        row.last_ok_at = _parse_iso(e.get("last_ok_at")) or row.last_ok_at
        row.reported_at = now()
        # Fire the moment a login CROSSES into failing -- the freshness scorecard
        # only reports this in a Monday-morning aggregate, so a specific account
        # going permanently dark got zero targeted alert until then (Ford,
        # 2026-07-08: "find every instance of us intentionally sabotaging our own
        # reliability"). Only on the transition, so a login stuck failing for
        # weeks doesn't re-alert every heartbeat.
        now_failing = bool(row.paused) or (row.fails or 0) >= _PAUSE_FAILS
        if now_failing and not was_failing:
            from .notify import send_internal_alert
            t = db.get(Tenant, tenant_id)
            send_internal_alert(
                f"Portal login failing: {provider} ({tenant_id})",
                f"Tenant: {(t.name if t else tenant_id)} ({tenant_id})\n"
                f"Provider: {provider}\nUsername: {username}\nFails: {row.fails}\n\n"
                "Auto-login has given up on this saved password -- it's almost "
                "certainly wrong/changed. The owner needs to re-save it in the "
                "extension vault.",
            )
    for provider, usernames in seen.items():
        stale_rows = db.execute(
            select(PortalLoginStatus).where(
                PortalLoginStatus.tenant_id == tenant_id,
                PortalLoginStatus.provider == provider,
            )
        ).scalars().all()
        for r in stale_rows:
            if r.username_lc not in usernames:
                db.delete(r)                     # removed from the vault → removed here


def _login_state(row: Optional[PortalLoginStatus]) -> str:
    """Roster state for one saved-login row (None = nothing saved)."""
    if row is None:
        return "login_missing"
    if row.paused or (row.fails or 0) >= _PAUSE_FAILS:
        return "failing"
    if not row.enabled:
        return "disabled"
    if row.last_ok_at and (now() - row.last_ok_at) <= _STALE_AFTER:
        return "automated"
    return "saved_pending"                       # saved; first/next pull hasn't landed yet


@router.get("/v1/portal-access")
def portal_access(authorization: Optional[str] = Header(default=None)):
    """Per-client portal automation roster for the dashboard "Portal access" tab.

    One row per (client, portal identity). A client with both a GMP and a co-op
    identity gets two rows. Clients with NO portal identity at all surface as
    status="no_portal_identity" — the actionable "collect this from the client"
    state. Saved logins no client claims are returned as unassigned_logins.
    """
    from .account import tenant_from_session
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        tenant = db.get(Tenant, t.id)
        hb = tenant.extension_heartbeat_at if tenant else None
        logins = db.execute(
            select(PortalLoginStatus).where(PortalLoginStatus.tenant_id == t.id)
        ).scalars().all()
        # username → row, per provider family (gmp vs smarthub co-ops). The
        # vec_* Client columns are shared by every SmartHub co-op, so a co-op
        # identity matches a login row from ANY non-gmp provider.
        gmp_by_user = {r.username_lc: r for r in logins if r.provider == "gmp"}
        coop_by_user = {r.username_lc: r for r in logins if r.provider != "gmp"}
        claimed: set[int] = set()
        rows: list[dict] = []
        clients = db.execute(
            select(Client).where(
                Client.tenant_id == t.id,
                Client.active.is_(True),
                Client.deleted_at.is_(None),
            ).order_by(Client.name)
        ).scalars().all()
        for c in clients:
            identities = []
            gmp_id = (c.gmp_email or c.gmp_username or "").strip()
            if gmp_id:
                identities.append(("gmp", gmp_id, gmp_by_user.get(gmp_id.lower()), c.gmp_last_sync_at))
            coop_id = (c.vec_email or c.vec_username or "").strip()
            if coop_id:
                identities.append(("smarthub", coop_id, coop_by_user.get(coop_id.lower()), c.vec_last_sync_at))
            if not identities:
                rows.append({
                    "client_id": c.id, "client": c.name, "provider": None,
                    "login_username": None, "status": "no_portal_identity",
                    "last_ok_at": None, "last_sync_at": None,
                    "enabled": None, "fails": 0,
                    "auto_send": bool(c.auto_send),
                })
                continue
            for provider, ident, login, last_sync in identities:
                if login is not None:
                    claimed.add(login.id)
                rows.append({
                    "client_id": c.id, "client": c.name,
                    "provider": login.provider if login else provider,
                    "login_username": ident,
                    "status": _login_state(login),
                    "last_ok_at": login.last_ok_at.isoformat() if login and login.last_ok_at else None,
                    "last_sync_at": last_sync.isoformat() if last_sync else None,
                    "enabled": bool(login.enabled) if login else None,
                    "fails": (login.fails or 0) if login else 0,
                    "auto_send": bool(c.auto_send),
                })
        unassigned = [{
            "provider": r.provider, "username": r.username,
            "status": _login_state(r),
            "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
            "enabled": bool(r.enabled), "fails": r.fails or 0,
        } for r in logins if r.id not in claimed]
    return {
        "extension_alive": bool(hb and (now() - hb) <= _EXT_ALIVE_WINDOW),
        "extension_last_seen": hb.isoformat() if hb else None,
        "clients": rows,
        "unassigned_logins": unassigned,
    }
