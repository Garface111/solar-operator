"""The server-side credential vault access layer.

The ONE place that touches decrypted portal passwords. Everything here is built
around two rules:
  1. Plaintext passwords are read just-in-time and NEVER logged or returned.
  2. Cloud Capture refuses to persist a password unless encryption-at-rest is
     actually armed (SO_CONFIG_KEY set) — we don't silently keep bare passwords.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from ..crypto import encryption_enabled
from ..models import HarvestRun, PortalCredential, PortalLoginStatus, now
from . import config

log = logging.getLogger("harvester.credentials")

# Auto-login pauses after this many consecutive failures (mirrors the extension's
# AUTOLOGIN_MAX_VENDOR_FAILS and portal_access._PAUSE_FAILS).
PAUSE_FAILS = 3


@dataclass
class Creds:
    tenant_id: str
    provider: str
    username: str
    # plaintext, JIT — NEVER log / NEVER appear in repr (Sentry locals)
    password: str = field(repr=False)
    login_host: str | None
    session_state: dict | None = field(repr=False, default=None)


def crypto_ready() -> bool:
    """True when SO_CONFIG_KEY is set so passwords encrypt at rest. Cloud Capture
    is gated on this — collection and harvesting both refuse without it."""
    return encryption_enabled()


def load_creds(cred: PortalCredential) -> Creds | None:
    """Decrypt a credential row into a JIT Creds bundle (transparent read via the
    EncryptedStr/EncryptedJSON column types). Returns None if the password is
    missing."""
    if not cred.secret_enc:
        return None
    # Tag decrypt audit logs with tenant/provider (never the password).
    try:
        from ..crypto import set_decrypt_audit_context, clear_decrypt_audit_context
        set_decrypt_audit_context(
            tenant_id=str(cred.tenant_id or ""),
            provider=str(cred.provider or ""),
            username_lc=str(getattr(cred, "username_lc", "") or ""),
        )
    except Exception:
        pass
    try:
        password = cred.secret_enc  # column decrypts here (vault audit fires)
        session_state = cred.session_state_enc
    finally:
        try:
            from ..crypto import clear_decrypt_audit_context
            clear_decrypt_audit_context()
        except Exception:
            pass
    return Creds(
        tenant_id=cred.tenant_id,
        provider=cred.provider,
        username=cred.username,
        password=password,
        login_host=cred.login_host,
        session_state=session_state,
    )


def upsert_credential(
    db,
    tenant_id: str,
    provider: str,
    username: str,
    password: str | None,
    login_host: str | None = None,
    enable: bool | None = None,
) -> PortalCredential:
    """Collect / update a server-side login. REQUIRES encryption to be armed when
    a password is provided — refuses to store a bare password.

    A None password leaves the existing secret untouched (used to flip the
    cloud_capture_enabled toggle without re-sending the password)."""
    if password and not crypto_ready():
        raise RuntimeError(
            "Refusing to store a portal password: SO_CONFIG_KEY is not set, so "
            "encryption-at-rest is off. Provision the key before collecting "
            "Cloud Capture credentials."
        )
    username_lc = (username or "").strip().lower()[:200]
    provider = (provider or "").strip().lower()[:40]
    row = db.execute(
        select(PortalCredential).where(
            PortalCredential.tenant_id == tenant_id,
            PortalCredential.provider == provider,
            PortalCredential.username_lc == username_lc,
        )
    ).scalar_one_or_none()
    if row is None:
        row = PortalCredential(
            tenant_id=tenant_id, provider=provider,
            username=username.strip()[:200], username_lc=username_lc,
        )
        db.add(row)
    row.username = username.strip()[:200]
    if password:
        row.secret_enc = password           # column encrypts on the way to the DB
        # Re-arm the lockout guard: a corrected password clears the fail-pause AND
        # the last-attempt timestamp so the scheduler treats it as never-harvested
        # and retries it NOW (else the fail-backoff on the stale timestamp holds
        # it). See scheduler._is_due / MAX_LOGIN_FAILS.
        row.harvest_fails = 0
        row.last_harvest_ok = None
        row.last_harvest_at = None
    if login_host is not None:
        row.login_host = login_host.strip()[:200] or None
    if enable is not None:
        row.cloud_capture_enabled = bool(enable)
    row.updated_at = now()
    return row


def save_session_state(db, cred: PortalCredential, storage_state: dict | None) -> None:
    """Persist the Playwright storage_state so the next run reuses a warm session
    instead of logging in again — the anti-lockout, anti-bot-detection lever."""
    if not storage_state:
        return
    cred.session_state_enc = storage_state
    cred.session_state_at = now()


def list_all_meta(db, *, limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    """Credential inventory for Sovereign/ops — metadata only, never secrets.

    ``tenant_id`` is REQUIRED for fleet safety. Pass an explicit tenant to list;
    fleet-wide inventory is refused here (callers that need cross-tenant ops must
    use an admin path with a separate hard gate, not the desk).
    """
    if not tenant_id:
        raise ValueError(
            "list_all_meta requires tenant_id — fleet-wide credential inventory "
            "is disabled (privilege-escalation guard)"
        )
    from sqlalchemy.orm import load_only

    q = (
        select(PortalCredential)
        .where(PortalCredential.tenant_id == tenant_id)
        .order_by(PortalCredential.updated_at.desc())
        .options(
            load_only(
                PortalCredential.id,
                PortalCredential.tenant_id,
                PortalCredential.provider,
                PortalCredential.username,
                PortalCredential.username_lc,
                PortalCredential.login_host,
                PortalCredential.cloud_capture_enabled,
                PortalCredential.last_harvest_at,
                PortalCredential.last_harvest_ok,
                PortalCredential.harvest_fails,
                PortalCredential.updated_at,
                # has_secret / has_session via IS NOT NULL expressions below —
                # still need the columns deferred: use raw null checks without
                # loading decrypted values via undefer of the encrypted cols.
            )
        )
    )
    rows = db.execute(q.limit(limit)).scalars().all()
    # Null-check encrypted columns without decrypting: issue a cheap companion
    # query for flags only (avoids EncryptedStr.process_result_value).
    ids = [r.id for r in rows]
    flags: dict[int, tuple[bool, bool]] = {}
    if ids:
        from sqlalchemy import bindparam, text as sa_text
        stmt = sa_text(
            "SELECT id, (secret_enc IS NOT NULL), (session_state_enc IS NOT NULL) "
            "FROM portal_credential WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        flag_rows = db.execute(stmt, {"ids": ids}).fetchall()
        for fr in flag_rows:
            flags[int(fr[0])] = (bool(fr[1]), bool(fr[2]))
    return [
        {
            "id": r.id,
            "tenant_id": r.tenant_id,
            "provider": r.provider,
            "username": r.username,
            "username_lc": r.username_lc,
            "login_host": r.login_host,
            "cloud_capture_enabled": bool(r.cloud_capture_enabled),
            "has_secret": flags.get(r.id, (False, False))[0],
            "has_session": flags.get(r.id, (False, False))[1],
            "last_harvest_at": r.last_harvest_at.isoformat() if r.last_harvest_at else None,
            "last_harvest_ok": r.last_harvest_ok,
            "harvest_fails": r.harvest_fails or 0,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def rearm(
    db,
    tenant_id: str,
    provider: str,
    username_lc: str | None = None,
    *,
    enable: bool | None = True,
) -> dict:
    """Clear fail/pause state so the scheduler retries this login ASAP.

    Used by Sovereign portal sign-off + credential unlock. Never returns secrets.
    """
    provider = (provider or "").strip().lower()
    q = select(PortalCredential).where(
        PortalCredential.tenant_id == tenant_id,
        PortalCredential.provider == provider,
    )
    if username_lc:
        q = q.where(PortalCredential.username_lc == username_lc.strip().lower())
    rows = db.execute(q).scalars().all()
    n = 0
    for r in rows:
        r.harvest_fails = 0
        r.last_harvest_at = None  # due immediately
        r.updated_at = now()
        if enable is not None:
            r.cloud_capture_enabled = bool(enable)
        # Mirror roster unpause
        roster = db.execute(
            select(PortalLoginStatus).where(
                PortalLoginStatus.tenant_id == tenant_id,
                PortalLoginStatus.provider == provider,
                PortalLoginStatus.username_lc == r.username_lc,
            )
        ).scalar_one_or_none()
        if roster:
            roster.paused = False
            roster.fails = 0
            roster.enabled = True
            roster.reported_at = now()
        n += 1
    db.flush()
    return {"ok": True, "rearmed": n, "tenant_id": tenant_id, "provider": provider}


def rearm_all(db, *, tenant_id: str | None = None, only_enabled: bool = False) -> int:
    """Re-arm vault credentials for ONE tenant. Fleet-wide rearm is refused.

    ``tenant_id`` is required (blast-radius guard). Returns count re-armed.
    """
    if not (tenant_id or "").strip():
        raise ValueError(
            "rearm_all requires tenant_id — fleet-wide rearm is disabled"
        )
    q = select(PortalCredential).where(PortalCredential.tenant_id == tenant_id)
    if only_enabled:
        q = q.where(PortalCredential.cloud_capture_enabled.is_(True))
    rows = db.execute(q).scalars().all()
    for r in rows:
        r.harvest_fails = 0
        r.last_harvest_at = None
        r.updated_at = now()
        roster = db.execute(
            select(PortalLoginStatus).where(
                PortalLoginStatus.tenant_id == r.tenant_id,
                PortalLoginStatus.provider == r.provider,
                PortalLoginStatus.username_lc == r.username_lc,
            )
        ).scalar_one_or_none()
        if roster:
            roster.paused = False
            roster.fails = 0
            roster.reported_at = now()
    db.flush()
    return len(rows)


def unpause_portal_login(
    db,
    tenant_id: str,
    provider: str,
    username_lc: str | None = None,
) -> dict:
    """Clear extension-roster pause (PortalLoginStatus) without touching secrets."""
    provider = (provider or "").strip().lower()
    q = select(PortalLoginStatus).where(
        PortalLoginStatus.tenant_id == tenant_id,
        PortalLoginStatus.provider == provider,
    )
    if username_lc:
        q = q.where(PortalLoginStatus.username_lc == username_lc.strip().lower())
    rows = db.execute(q).scalars().all()
    for r in rows:
        r.paused = False
        r.fails = 0
        r.enabled = True
        r.reported_at = now()
    db.flush()
    return {"ok": True, "unpaused": len(rows), "tenant_id": tenant_id, "provider": provider}


def record_health(
    db,
    cred: PortalCredential,
    *,
    ok: bool,
    status: str,
    started_at: datetime,
    fresh_login: bool = False,
    rows_written: int = 0,
    error: str | None = None,
    screenshot_ref: str | None = None,
) -> None:
    """Update the credential's health, mirror it into the PortalLoginStatus roster
    (so the dashboard shows one unified view across extension + cloud capture),
    and append a HarvestRun audit row."""
    # Only a LOGIN failure counts toward the pause/backoff lockout guard — that's
    # the one that risks the utility/vendor locking the account. A scrape failure
    # happens AFTER we're authenticated, so it is not a lockout risk: record it as
    # not-ok but don't bump the fail counter, so it retries on the normal cadence
    # instead of the 30-min login backoff.
    login_failure = (status == "login_failed")
    cred.last_harvest_at = now()
    cred.last_harvest_ok = ok
    # `harvest_fails` counts CONSECUTIVE FRESH-LOGIN failures — nothing else may
    # clear it. A warm-session success proves the stored cookie/token still
    # works; it proves NOTHING about the password, so it must not wipe a standing
    # login problem. (Bug, live 2026-07-19: `if ok:` cleared the counter on every
    # warm tick, so a credential alternating login_failed → ok never reached
    # MAX_LOGIN_FAILS and the whole lockout guard was dead code — Bruce's SMA
    # login sat at harvest_fails=1 while eating 79 failed logins a day.)
    if ok and fresh_login:
        cred.harvest_fails = 0
    elif login_failure:
        cred.harvest_fails = min((cred.harvest_fails or 0) + 1, 999)

    # Mirror into the roster row the dashboard reads.
    row = db.execute(
        select(PortalLoginStatus).where(
            PortalLoginStatus.tenant_id == cred.tenant_id,
            PortalLoginStatus.provider == cred.provider,
            PortalLoginStatus.username_lc == cred.username_lc,
        )
    ).scalar_one_or_none()
    if row is None:
        row = PortalLoginStatus(
            tenant_id=cred.tenant_id, provider=cred.provider,
            username=cred.username, username_lc=cred.username_lc,
        )
        db.add(row)
    if ok:
        row.last_ok_at = now()
        # Same rule as above: data flowing on a warm session is real (stamp
        # last_ok_at), but only a successful FRESH login clears the pause — the
        # roster must not show "healthy login" on the strength of a warm cookie.
        if fresh_login:
            row.fails = 0
            row.paused = False
    elif login_failure:
        row.fails = min((row.fails or 0) + 1, 999)
        if row.fails >= PAUSE_FAILS:
            row.paused = True
    row.reported_at = now()

    # Audit row: always write failures + fresh password logins. Warm-session
    # OK ticks fire every few minutes for inverters (~5k/day fleet-wide) and
    # drown the table — throttle those to at most one per credential per hour.
    write_audit = True
    if ok and status == "ok" and not fresh_login:
        from datetime import timedelta
        recent = db.execute(
            select(HarvestRun.id)
            .where(
                HarvestRun.tenant_id == cred.tenant_id,
                HarvestRun.provider == cred.provider,
                HarvestRun.username_lc == cred.username_lc,
                HarvestRun.status == "ok",
                HarvestRun.logged_in_fresh.is_(False),
                HarvestRun.started_at >= now() - timedelta(hours=1),
            )
            .limit(1)
        ).scalar_one_or_none()
        if recent is not None:
            write_audit = False
    if write_audit:
        db.add(HarvestRun(
            tenant_id=cred.tenant_id, provider=cred.provider,
            username_lc=cred.username_lc, started_at=started_at, ended_at=now(),
            status=status, logged_in_fresh=fresh_login, rows_written=rows_written,
            detail=(error or None), screenshot_ref=screenshot_ref,
        ))
    # Deliberately NO password in any log line.
    log.info("harvest %s provider=%s tenant=%s fresh=%s rows=%d audit=%s",
             status, cred.provider, cred.tenant_id, fresh_login, rows_written,
             write_audit)
