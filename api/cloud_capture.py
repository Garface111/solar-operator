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
from .models import PortalCredential, PortalLoginStatus, now
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
        rows = [{
            "provider": c.provider,
            "username": c.username,
            "enabled": bool(c.cloud_capture_enabled),
            "login_host": c.login_host,
            "last_harvest_at": c.last_harvest_at.isoformat() if c.last_harvest_at else None,
            "last_harvest_ok": c.last_harvest_ok,
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
    # A co-op needs its host to know which portal to open.
    if provider not in ("gmp",) and provider not in (
            "fronius", "sma", "chint", "solaredge") and not (body.login_host or "").strip():
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
