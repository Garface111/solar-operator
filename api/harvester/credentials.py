"""The server-side credential vault access layer.

The ONE place that touches decrypted portal passwords. Everything here is built
around two rules:
  1. Plaintext passwords are read just-in-time and NEVER logged or returned.
  2. Cloud Capture refuses to persist a password unless encryption-at-rest is
     actually armed (SO_CONFIG_KEY set) — we don't silently keep bare passwords.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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
    password: str            # plaintext, JIT — never log this
    login_host: str | None
    session_state: dict | None


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
    return Creds(
        tenant_id=cred.tenant_id,
        provider=cred.provider,
        username=cred.username,
        password=cred.secret_enc,          # already decrypted by the column type
        login_host=cred.login_host,
        session_state=cred.session_state_enc,
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
    cred.last_harvest_at = now()
    cred.last_harvest_ok = ok
    cred.harvest_fails = 0 if ok else min((cred.harvest_fails or 0) + 1, 999)

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
        row.fails = 0
        row.paused = False
    else:
        row.fails = min((row.fails or 0) + 1, 999)
        if row.fails >= PAUSE_FAILS:
            row.paused = True
    row.reported_at = now()

    db.add(HarvestRun(
        tenant_id=cred.tenant_id, provider=cred.provider,
        username_lc=cred.username_lc, started_at=started_at, ended_at=now(),
        status=status, logged_in_fresh=fresh_login, rows_written=rows_written,
        detail=(error or None), screenshot_ref=screenshot_ref,
    ))
    # Deliberately NO password in any log line.
    log.info("harvest %s provider=%s tenant=%s fresh=%s rows=%d",
             status, cred.provider, cred.tenant_id, fresh_login, rows_written)
