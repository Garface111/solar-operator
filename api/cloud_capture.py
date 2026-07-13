"""Cloud Capture API — collect server-side portal credentials and report status.

The customer-facing seam for the "hands-off, server-side" refresh option. The
extension keeps the client-side path; THIS lets an owner opt a login into Cloud
Capture by handing us the password once (encrypted at rest, decrypted only in the
harvester). Security invariants enforced here:
  * a password is accepted only when encryption-at-rest is armed (SO_CONFIG_KEY);
  * no endpoint ever returns a stored password;
  * "delete" is a HARD delete — the ciphertext row is removed.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from .account import tenant_from_session
from .db import SessionLocal
from .models import PortalCredential, PortalLoginStatus, HarvestRun, now
from .harvester import config
from .harvester import credentials as cc
from . import ratelimit

router = APIRouter()


class CredentialIn(BaseModel):
    provider: str
    username: str
    password: Optional[str] = None      # omit to toggle without re-sending
    login_host: Optional[str] = None    # SmartHub co-op subdomain (required for co-ops)
    enable: Optional[bool] = True
    consent: Optional[bool] = None       # explicit opt-in; REQUIRED to store a password


class ToggleIn(BaseModel):
    provider: str
    username: str
    enable: bool


class DeleteIn(BaseModel):
    provider: str
    username: str


def _mirror_roster(db, tenant_id: str, provider: str, username: str, rearm: bool = False) -> None:
    """Make the login visible in the dashboard's Portal-access roster immediately
    (the same table the extension heartbeat populates), so both capture methods
    show one unified list. When `rearm` (a fresh password was saved), clear any
    fail-pause so the login is retried (mirrors the scheduler's re-arm)."""
    username_lc = username.strip().lower()
    row = db.execute(
        select(PortalLoginStatus).where(
            PortalLoginStatus.tenant_id == tenant_id,
            PortalLoginStatus.provider == provider.lower(),
            PortalLoginStatus.username_lc == username_lc,
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(PortalLoginStatus(
            tenant_id=tenant_id, provider=provider.lower(),
            username=username.strip(), username_lc=username_lc, reported_at=now(),
        ))
    elif rearm:
        row.paused = False
        row.fails = 0
        row.reported_at = now()


@router.get("/v1/cloud-capture/status")
def status(authorization: Optional[str] = Header(default=None)):
    """Per-login Cloud-Capture state for the Auto-refresh panel. No secrets."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        creds = db.execute(
            select(PortalCredential).where(PortalCredential.tenant_id == t.id)
        ).scalars().all()
        # Distinguish WHY the last harvest wasn't ok — a bare last_harvest_ok=false
        # can't tell "wrong password" (status=login_failed, the only real credential
        # issue) from "signed in fine, the post-login data pull hit a transient
        # snag" (status=scrape_failed — doesn't even count toward the fail/pause
        # counter, see credentials.record_health). Without this the panel told
        # owners to "check the password" for hiccups that had nothing to do with
        # their password (Ford 2026-07-12, Chint). One query, newest-first, indexed
        # on (tenant_id, provider, started_at) — take the first row per credential.
        last_status: dict[tuple[str, str], str] = {}
        recent_runs = db.execute(
            select(HarvestRun.provider, HarvestRun.username_lc, HarvestRun.status)
            .where(HarvestRun.tenant_id == t.id)
            .order_by(HarvestRun.started_at.desc())
            .limit(200)
        ).all()
        for provider, username_lc, run_status in recent_runs:
            key = (provider, username_lc)
            if key not in last_status:
                last_status[key] = run_status
        rows = [{
            "provider": c.provider,
            "username": c.username,
            "enabled": bool(c.cloud_capture_enabled),
            "login_host": c.login_host,
            "last_harvest_at": c.last_harvest_at.isoformat() if c.last_harvest_at else None,
            "last_harvest_ok": c.last_harvest_ok,
            "last_harvest_status": last_status.get((c.provider, c.username_lc)),
            "harvest_fails": c.harvest_fails or 0,
            "has_session": bool(c.session_state_enc),
        } for c in creds]
    return {
        "encryption_ready": cc.crypto_ready(),
        "collection_enabled": config.collection_enabled(),
        "harvesting_enabled": config.enabled(),
        "credentials": rows,
    }


@router.post("/v1/cloud-capture/credentials")
def save_credential(body: CredentialIn, request: Request,
                    authorization: Optional[str] = Header(default=None)):
    """Collect / update a server-side login. Refuses a password without encryption."""
    t = tenant_from_session(authorization)
    # Bound password-collection abuse (stolen session dumping many portals).
    ratelimit.enforce(request, "cloud_capture_save", max_hits=20, window_s=3600,
                      key_extra=t.id,
                      message="Too many credential saves — try again later.")
    if not config.collection_enabled():
        raise HTTPException(403, "Cloud Capture credential collection is not enabled.")
    if body.password and not cc.crypto_ready():
        raise HTTPException(
            409, "Server-side credential storage is not armed (encryption key unset). "
                 "Cloud Capture cannot accept passwords yet.")
    # Explicit opt-in consent is REQUIRED before we store a password server-side
    # (the trust-model reversal — see terms.html §3 / privacy.html). A toggle-only
    # save (no new password) doesn't re-collect consent.
    if body.password and not body.consent:
        raise HTTPException(
            422, "Storing a password with Cloud Capture requires your explicit consent.")
    if body.password:
        import logging
        logging.getLogger("cloud_capture").info(
            "cloud-capture consent recorded: tenant=%s provider=%s", t.id, body.provider)
    provider = (body.provider or "").strip().lower()
    if not provider or not (body.username or "").strip():
        raise HTTPException(422, "provider and username are required")
    # A co-op needs its host to know which portal to open. Bespoke utilities
    # (GMP, Eversource) and inverter clouds have a fixed login URL in the
    # harvester module — no login_host required.
    _no_host = {
        "gmp", "eversource", "eversource_ma", "eversource_ct", "cmp",
        "fronius", "sma", "chint", "solaredge",
    }
    if provider not in _no_host and not (body.login_host or "").strip():
        raise HTTPException(422, "login_host (co-op subdomain) is required for SmartHub co-ops")
    with SessionLocal() as db:
        cc.upsert_credential(
            db, t.id, provider, body.username, body.password,
            login_host=body.login_host, enable=body.enable,
        )
        _mirror_roster(db, t.id, provider, body.username, rearm=bool(body.password))
        db.commit()
    return {"ok": True, "provider": provider, "username": body.username.strip(),
            "enabled": bool(body.enable), "encryption_ready": cc.crypto_ready()}


@router.post("/v1/cloud-capture/toggle")
def toggle(body: ToggleIn, authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        row = db.execute(
            select(PortalCredential).where(
                PortalCredential.tenant_id == t.id,
                PortalCredential.provider == body.provider.strip().lower(),
                PortalCredential.username_lc == body.username.strip().lower(),
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "No such Cloud Capture login")
        row.cloud_capture_enabled = bool(body.enable)
        row.updated_at = now()
        db.commit()
    return {"ok": True, "enabled": bool(body.enable)}


@router.delete("/v1/cloud-capture/credentials")
def delete_credential(body: DeleteIn, authorization: Optional[str] = Header(default=None)):
    """HARD-delete the server-side credential (removes the encrypted password and
    the persisted session). The extension's own client-side vault is untouched."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        row = db.execute(
            select(PortalCredential).where(
                PortalCredential.tenant_id == t.id,
                PortalCredential.provider == body.provider.strip().lower(),
                PortalCredential.username_lc == body.username.strip().lower(),
            )
        ).scalar_one_or_none()
        if row is not None:
            db.delete(row)
            db.commit()
    return {"ok": True}


@router.post("/v1/cloud-capture/refresh")
def refresh_now(authorization: Optional[str] = Header(default=None)):
    """Force a fresh server-side capture of every enabled Cloud Capture login for
    this tenant NOW — the cloud-mode counterpart of the extension's 'Sync all
    vendors' (Ford 2026-07-11). It re-arms each credential (clears last_harvest_at
    + the fail backoff) so the harvester picks it up on its very next tick (≤90s),
    reusing the warm session — no fresh login, so no 'suspicious sign-in' risk. The
    client then re-pulls the fleet data to show the updated readings.
    """
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        rows = db.execute(
            select(PortalCredential).where(
                PortalCredential.tenant_id == t.id,
                PortalCredential.cloud_capture_enabled.is_(True),
                PortalCredential.secret_enc.isnot(None),
            )
        ).scalars().all()
        for r in rows:
            r.last_harvest_at = None
            r.harvest_fails = 0
            r.updated_at = now()
        db.commit()
    return {"ok": True, "queued": len(rows)}
