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

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

# Loading a PortalCredential ENTITY decrypts its vault columns at row-fetch time
# (crypto.EncryptedVaultStr.process_result_value), and the public web process is
# forbidden to unwrap portal passwords (SO_VAULT_DECRYPT=0) — so every
# management endpoint here raised
#   RuntimeError: Vault decrypt is disabled in this process
# 1,674 times, and each one also paged Ford with a CRITICAL alert. Toggle,
# delete AND refresh were all 500ing in production: the whole Cloud Capture
# management UI was dead, not just noisy (Ford 2026-07-20).
#
# These endpoints only ever touch METADATA — enabled flag, fail counters,
# timestamps, or the row's existence. Deferring the vault columns means the
# secret is never fetched, so nothing decrypts. The guard stays meaningful:
# anything that genuinely touches .secret_enc still trips it, loudly.
# (defined below, once PortalCredential is imported)

from .account import tenant_from_session
from .db import SessionLocal
from .models import Client, PortalCredential, PortalLoginStatus, HarvestRun, now

# See the note above: never fetch the vault columns in the web process.
_NO_VAULT = (defer(PortalCredential.secret_enc),
             defer(PortalCredential.session_state_enc))
from .harvester import config
from .harvester import credentials as cc
from . import ratelimit

log = logging.getLogger("cloud_capture")

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


def _name_from_login(login: str) -> str:
    """Best-effort human name from a login string — mirrors _smart_client_name's
    email-local-part fallback (john.doe@x → 'John Doe'). Upgraded to the real
    portal holder name on the first successful capture."""
    s = (login or "").strip()
    local = s.split("@")[0] if "@" in s else s
    cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return (cleaned.title()[:200] or s[:200] or "New client")


def ensure_client_for_login(db, tenant, provider: str, username: str) -> None:
    """Eagerly create (or reuse) a Client for a Cloud Capture utility login so the
    Clients page shows it IMMEDIATELY — a 'Pulling bills…' card — instead of
    staying empty until the harvester's first capture lands (Ford 2026-07-16).

    Correctness hinge: we set the SAME login columns + autopop flag the /v1/sync
    matcher keys on (api/app.py _PROVIDER_AUTOPOP_FIELDS), so when the harvester
    later POSTs the captured bills the matcher ATTACHES arrays to THIS client
    rather than auto-creating a duplicate. `capture_pending` marks it as awaiting
    that first capture (drives the UI state + the name upgrade in the matcher).

    Scope: tenants in the generation-reports world — NEPOOL tenants, and (post
    THE FOLD) Array Operator tenants with generation_reports set. A regular AO
    tenant uses offtakers, not sub-clients, so it's skipped. Only providers the
    capture path can autopopulate — GMP + SmartHub co-ops. Inverter clouds
    (fronius/sma/chint/solaredge) and unmapped bespoke utilities have no autopop
    config, so a pre-created card would never fill in; skip them.
    """
    from .report_eligibility import tenant_in_reports_world  # noqa: PLC0415
    if not tenant_in_reports_world(tenant):
        return
    login = (username or "").strip()
    if not login:
        return
    # Single source of truth for provider → Client login columns. Lazy import
    # avoids the app↔cloud_capture module cycle (app includes this router).
    from .app import _PROVIDER_AUTOPOP_FIELDS  # noqa: PLC0415
    cfg = _PROVIDER_AUTOPOP_FIELDS.get(provider.strip().lower())
    if not cfg:
        return

    login_lc = login.lower()
    is_email = "@" in login

    def _bind(c) -> None:
        """Stamp the login onto the Client using the columns the /v1/sync matcher
        keys on, arm autopop, and mark it awaiting its first bill."""
        setattr(c, cfg["username_attr"], login)
        setattr(c, cfg["autopop_attr"], True)
        if is_email:
            setattr(c, cfg["email_attr"], login_lc)
            if not c.contact_email:
                c.contact_email = login_lc
        c.capture_pending = True

    match_terms = [func.lower(cfg["username_col"]) == login_lc]
    if is_email:
        match_terms.append(func.lower(cfg["email_col"]) == login_lc)
    existing = db.execute(
        select(Client).where(
            Client.tenant_id == tenant.id,
            Client.deleted_at.is_(None),
            or_(*match_terms),
        ).order_by(Client.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        # Operator already has a client on this login (manual entry, prior
        # capture, or a re-save). Just make sure autopop is armed so the harvest
        # attaches — never stomp their curated name / contact.
        if not getattr(existing, cfg["autopop_attr"]):
            setattr(existing, cfg["autopop_attr"], True)
        return

    base_name = _name_from_login(login)

    # Adopt the blank "Your first client" placeholder that onboarding seeds at
    # activation (api/onboarding.ensure_placeholder_client) — exactly as the
    # /v1/sync capture path does — so N cloud logins yield N client cards (the
    # first reusing the placeholder) instead of N + a stray empty placeholder.
    # Only a placeholder with NO login bound to either provider family is
    # adoptable, so a second login never stomps the first login's card.
    target = db.execute(
        select(Client).where(
            Client.tenant_id == tenant.id,
            Client.deleted_at.is_(None),
            Client.is_placeholder.is_(True),
            Client.gmp_email.is_(None), Client.gmp_username.is_(None),
            Client.vec_email.is_(None), Client.vec_username.is_(None),
        ).order_by(Client.id).limit(1)
    ).scalar_one_or_none()

    # Suffix on the (tenant_id, name) unique constraint — two logins can derive
    # the same display name, and the constraint doesn't exclude soft-deleted
    # rows. Retry inside a SAVEPOINT so a collision never rolls back the
    # credential save that shares this outer transaction.
    for attempt in range(20):
        name = base_name if attempt == 0 else f"{base_name} {attempt + 1}"
        try:
            with db.begin_nested():
                if target is not None:
                    target.is_placeholder = False
                    _bind(target)
                    if target.name_edited_at is None:
                        target.name = name
                else:
                    c = Client(tenant_id=tenant.id, name=name, active=True)
                    _bind(c)
                    db.add(c)
                db.flush()
            return
        except IntegrityError:
            if target is not None and target.name_edited_at is not None:
                return  # curated name we won't rename — nothing left to retry
            continue
    log.warning("ensure_client_for_login: couldn't bind client for %s/%s after 20 tries",
                tenant.id, provider)


@router.get("/v1/cloud-capture/status")
def status(authorization: Optional[str] = Header(default=None)):
    """Per-login Cloud-Capture state for the Auto-refresh panel. No secrets."""
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        # Select only the columns the panel needs — NEVER the encrypted vault
        # columns (secret_enc / session_state_enc). Materializing the full ORM
        # entity would run their decrypting TypeDecorators at row-fetch time,
        # which the public web process refuses (SO_VAULT_DECRYPT=0). "has_session"
        # is derived at the SQL layer so the ciphertext is never touched.
        creds = db.execute(
            select(
                PortalCredential.provider,
                PortalCredential.username,
                PortalCredential.username_lc,
                PortalCredential.cloud_capture_enabled,
                PortalCredential.login_host,
                PortalCredential.last_harvest_at,
                PortalCredential.last_harvest_ok,
                PortalCredential.harvest_fails,
                PortalCredential.session_state_enc.isnot(None).label("has_session"),
            ).where(PortalCredential.tenant_id == t.id)
        ).all()
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

        # T2-1: customer-visible harvest activity (trust + tripwire). Counts only
        # — never detail text, never secrets. Month = UTC calendar month.
        from datetime import datetime
        from sqlalchemy import func, case
        month_start = datetime.utcnow().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        activity_rows = db.execute(
            select(
                HarvestRun.provider,
                HarvestRun.username_lc,
                func.count().label("attempts"),
                func.coalesce(
                    func.sum(
                        case((HarvestRun.logged_in_fresh.is_(True), 1), else_=0)
                    ),
                    0,
                ).label("fresh_logins"),
                func.coalesce(
                    func.sum(case((HarvestRun.status == "ok", 1), else_=0)),
                    0,
                ).label("ok"),
            )
            .where(
                HarvestRun.tenant_id == t.id,
                HarvestRun.started_at >= month_start,
            )
            .group_by(HarvestRun.provider, HarvestRun.username_lc)
        ).all()
        activity: dict[tuple[str, str], dict] = {
            (p, u): {
                "attempts_this_month": int(a or 0),
                "sign_ins_this_month": int(f or 0),  # full username/password logins
                "ok_this_month": int(o or 0),
            }
            for p, u, a, f, o in activity_rows
        }

        rows = []
        for c in creds:
            act = activity.get((c.provider, c.username_lc), {
                "attempts_this_month": 0,
                "sign_ins_this_month": 0,
                "ok_this_month": 0,
            })
            rows.append({
                "provider": c.provider,
                "username": c.username,
                "enabled": bool(c.cloud_capture_enabled),
                "login_host": c.login_host,
                "last_harvest_at": c.last_harvest_at.isoformat() if c.last_harvest_at else None,
                "last_harvest_ok": c.last_harvest_ok,
                "last_harvest_status": last_status.get((c.provider, c.username_lc)),
                "harvest_fails": c.harvest_fails or 0,
                "has_session": bool(c.has_session),
                **act,
            })
    return {
        "encryption_ready": cc.crypto_ready(),
        "collection_enabled": config.collection_enabled(),
        "harvesting_enabled": config.enabled(),
        "credentials": rows,
        "activity_month": month_start.strftime("%Y-%m"),
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
        log.info("cloud-capture consent recorded: tenant=%s provider=%s", t.id, body.provider)
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
        # Eagerly surface a Client per utility login so the operator's Clients
        # page fills in the moment they connect a login — the harvested bills
        # attach to it on the first capture (Ford 2026-07-16). Convenience only:
        # never let a client-mirror hiccup fail the credential save.
        if body.enable:
            try:
                ensure_client_for_login(db, t, provider, body.username)
            except Exception:  # noqa: BLE001
                log.warning("ensure_client_for_login failed for %s/%s", t.id, provider,
                            exc_info=True)
        db.commit()
    return {"ok": True, "provider": provider, "username": body.username.strip(),
            "enabled": bool(body.enable), "encryption_ready": cc.crypto_ready()}


@router.post("/v1/cloud-capture/toggle")
def toggle(body: ToggleIn, authorization: Optional[str] = Header(default=None)):
    t = tenant_from_session(authorization)
    with SessionLocal() as db:
        row = db.execute(
            select(PortalCredential).options(*_NO_VAULT).where(
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
            select(PortalCredential).options(*_NO_VAULT).where(
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
        # secret_enc.isnot(None) stays — it is a SQL predicate, evaluated in
        # Postgres, and never fetches (or decrypts) the column.
        rows = db.execute(
            select(PortalCredential).options(*_NO_VAULT).where(
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
