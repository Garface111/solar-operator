"""Enumerate due Cloud-Capture work and run a tick.

Due work = opted-in credentials whose last successful harvest is older than the
family cadence (utilities are monthly-bill data, so ~12h is plenty; never poll a
utility on a live cadence — it just invites lockouts). Safe by default: the
global switch gates everything, and until CLOUD_CAPTURE_REAL_CUSTOMERS is set the
farm only touches demo/test tenants or an explicit tenant allowlist.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from sqlalchemy import select

from ..db import SessionLocal
from ..models import PortalCredential, Tenant, now
from . import config

log = logging.getLogger("harvester.scheduler")

# Per-family cadence. Vendor (inverter) data is LIVE production power — Ford's
# hard SLA is "never more than 5 minutes old", so inverters re-harvest on a tight
# loop (warm session ⇒ each cycle is just navigate + read + POST, no re-login).
# Utility bills are monthly, so ~12h is plenty and keeps us gentle on portals.
# Both env-tunable; keep INVERTER_DUE + tick_seconds well under 5 min combined.
INVERTER_CODES = {"fronius", "sma", "chint", "solaredge", "solis", "enphase",
                  "tigo", "alsoenergy", "locus"}
INVERTER_DUE = timedelta(seconds=int(os.environ.get("CLOUD_CAPTURE_INVERTER_DUE_SECONDS") or 180))
UTILITY_DUE = timedelta(hours=int(os.environ.get("CLOUD_CAPTURE_DUE_HOURS") or 12))


def _due_after(provider: str) -> timedelta:
    return INVERTER_DUE if (provider or "").lower() in INVERTER_CODES else UTILITY_DUE


def _tenant_allowlist() -> set[str]:
    raw = os.environ.get("CLOUD_CAPTURE_TENANTS") or ""
    return {t.strip() for t in raw.split(",") if t.strip()}


def _tenant_allowed(db, tenant_id: str, allow_real: bool, allowlist: set[str]) -> bool:
    """Gate harvest by tenant lifecycle + real-customer switch.

    ALWAYS refuses inactive tenants — cancelled customers must never be logged
    into after churn (legal exposure, not just product hygiene). The real-
    customer / demo / allowlist gates only apply among active tenants.
    """
    t = db.get(Tenant, tenant_id)
    if t is None:
        return False
    # Hard stop: churned / purged / cancelled → never harvest.
    if not bool(getattr(t, "active", False)):
        return False
    if allow_real:
        return True
    if tenant_id in allowlist:
        return True
    return bool(getattr(t, "is_demo", False))


# ── Lockout safety (the hard constraint: never lock a customer out of their own
# portal). A wrong/changed password or an MFA wall makes every login fail; retrying
# that on the tight inverter loop is exactly how a utility's own lockout policy
# trips. So we STOP after MAX_LOGIN_FAILS and, before that, back off hard between
# attempts instead of hammering. A login only re-arms when the owner re-saves it.
MAX_LOGIN_FAILS = 3
FAIL_BACKOFF = timedelta(minutes=int(os.environ.get("CLOUD_CAPTURE_FAIL_BACKOFF_MIN") or 30))


def _is_due(c, _now) -> bool:
    """Whether a credential should be harvested now — with the lockout guard."""
    fails = c.harvest_fails or 0
    if fails >= MAX_LOGIN_FAILS:
        return False                                  # PAUSED — never hammer a bad login
    last = c.last_harvest_at
    if last is None:
        return True                                   # never harvested
    if fails > 0:
        # LOGIN failures → escalating backoff (30m, 60m, …), the lockout guard.
        return last <= _now - FAIL_BACKOFF * fails
    # fails==0: healthy OR a post-login scrape failure (not a lockout risk) →
    # retry on the normal family cadence, not the login backoff.
    return last <= _now - _due_after(c.provider)


def due_credentials() -> list[tuple[str, str, str]]:
    """(tenant_id, provider, username_lc) for every credential due a harvest.

    IMPORTANT: never SELECT secret_enc / session_state_enc here. EncryptedStr
    decrypts on row-fetch, and a fleet-wide due-scan used to unwrap the entire
    vault every tick (~2.8k full-vault decrypts/day) just to evaluate cadence.
    Decrypt happens just-in-time inside the harvest job via load_creds().
    """
    if not config.enabled():
        return []
    from sqlalchemy.orm import load_only

    allow_real = config.allow_real_customers()
    allowlist = _tenant_allowlist()
    _now = now()
    out: list[tuple[str, str, str]] = []
    with SessionLocal() as db:
        # Scheduling columns only + IS NOT NULL on secret (no decrypt).
        # Join active tenants in SQL so inactive never even reach Python.
        rows = db.execute(
            select(PortalCredential)
            .join(Tenant, Tenant.id == PortalCredential.tenant_id)
            .where(
                PortalCredential.cloud_capture_enabled.is_(True),
                PortalCredential.secret_enc.isnot(None),
                Tenant.active.is_(True),
            )
            .options(
                load_only(
                    PortalCredential.id,
                    PortalCredential.tenant_id,
                    PortalCredential.provider,
                    PortalCredential.username_lc,
                    PortalCredential.harvest_fails,
                    PortalCredential.last_harvest_at,
                    PortalCredential.cloud_capture_enabled,
                )
            )
        ).scalars().all()
        # Cache allow decisions per tenant_id (demo/allowlist still need Tenant).
        allowed_cache: dict[str, bool] = {}
        for c in rows:
            if not _is_due(c, _now):
                continue
            tid = c.tenant_id
            if tid not in allowed_cache:
                allowed_cache[tid] = _tenant_allowed(db, tid, allow_real, allowlist)
            if not allowed_cache[tid]:
                continue
            out.append((tid, c.provider, c.username_lc))
    return out


async def run_tick(farm) -> list:
    """Harvest all due credentials concurrently (bounded by the farm semaphore)."""
    jobs = due_credentials()
    if not jobs:
        log.info("tick: nothing due")
        return []
    log.info("tick: %d due", len(jobs))
    results = await asyncio.gather(
        *[farm.harvest(t, p, u) for (t, p, u) in jobs],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if getattr(r, "status", None) == "ok")
    log.info("tick done: %d/%d ok", ok, len(results))
    return results


async def run_forever():
    """Long-running loop: build one BrowserFarm and tick on the configured cadence."""
    from .engine import BrowserFarm
    interval = config.tick_seconds()
    async with BrowserFarm() as farm:
        while True:
            try:
                await run_tick(farm)
            except Exception as exc:                  # noqa: BLE001 — never die on a tick
                log.exception("tick error: %s", exc)
            await asyncio.sleep(interval)
