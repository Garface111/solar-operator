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
from ..models import HarvestRun, PortalCredential, Tenant, now
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
# trips. So before MAX_LOGIN_FAILS we back off hard between attempts, and past it
# we drop to a SLOW heartbeat instead of the tight loop.
#
# Reliability doctrine (Ford, memory: no-self-sabotage-reliability-audit): backing
# off here is legitimate ONLY because a real vendor lockout is at stake — so it is
# retry-forever-with-backoff, NEVER give-up-forever, and it is LOUD. The pause used
# to return False unconditionally: a paused login went silent forever and re-armed
# only if the owner happened to re-save it. That is a silent self-disarm. Now it
# retries every PAUSED_RETRY (hours, not never) and `lockout_alert` emails the
# operator while it stays paused.
MAX_LOGIN_FAILS = 3
FAIL_BACKOFF = timedelta(minutes=int(os.environ.get("CLOUD_CAPTURE_FAIL_BACKOFF_MIN") or 30))
# Slow heartbeat for a paused login. Long enough that no portal's lockout policy
# could care (a handful of attempts a day), short enough that a password the owner
# fixed at the portal — or a vendor-side outage that ended — heals itself.
PAUSED_RETRY = timedelta(hours=int(os.environ.get("CLOUD_CAPTURE_PAUSED_RETRY_HOURS") or 6))


def account_key(provider: str, username_lc: str) -> tuple[str, str]:
    """The unit a vendor actually locks out: ONE portal account.

    The same vendor login is routinely stored under several tenants (Bruce owns
    ~8 tenants; his one Chint login exists 8×, his SMA login 3×). Counting fails
    per-ROW made the guard per-tenant while the lockout is per-ACCOUNT — N tenants
    each burned their own 3 attempts against a single provider account. Fails and
    the pause are therefore coordinated on (provider, username_lc), globally.
    """
    return ((provider or "").strip().lower(), (username_lc or "").strip().lower())


def _is_due(c, _now, account_fails: int | None = None) -> bool:
    """Whether a credential should be harvested now — with the lockout guard.

    ``account_fails`` is the worst consecutive-fresh-login-failure count across
    EVERY credential row sharing this portal account (see `account_key`). Callers
    that don't pass it fall back to this row's own counter.
    """
    fails = c.harvest_fails or 0
    if account_fails is not None:
        fails = max(fails, account_fails)
    last = c.last_harvest_at
    if last is None:
        return True                                   # never harvested
    if fails >= MAX_LOGIN_FAILS:
        # PAUSED — slow heartbeat, never a permanent stop. Loudly alerted by
        # `lockout_alert.run_login_lockout_watchdog` for as long as it persists.
        return last <= _now - PAUSED_RETRY
    if fails > 0 and c.last_harvest_ok is False:
        # A standing login problem (the LAST run failed) → escalating backoff
        # (30m, 60m, …), the lockout guard. Gated on last_harvest_ok so a warm
        # session that is still serving data keeps its normal cadence instead of
        # being throttled to 30-min by an old login failure it already recovered
        # from — the counter stays set (honest), the data path stays fast.
        return last <= _now - FAIL_BACKOFF * fails
    # Healthy, or a post-login scrape failure (not a lockout risk) → retry on the
    # normal family cadence, not the login backoff.
    return last <= _now - _due_after(c.provider)


def accounts_with_recent_fresh_login(db, window: timedelta | None = None) -> set[tuple[str, str]]:
    """Portal accounts that let us in with a real password login inside `window`.

    This is the evidence that an account is NOT heading for a lockout: the portal
    accepted our credentials recently, which in every lockout policy worth the
    name also resets ITS failure counter. Read off `harvest_run` rather than
    `PortalCredential.last_harvest_ok` because that column only remembers the
    single most recent run — a login that succeeds intermittently (SMA does, ~1
    in 2) reads as "currently failing" between successes, which is true but is
    NOT grounds for pausing the whole account.
    """
    cutoff = now() - (window or PAUSED_RETRY)
    rows = db.execute(
        select(HarvestRun.provider, HarvestRun.username_lc)
        .where(HarvestRun.status == "ok",
               HarvestRun.logged_in_fresh.is_(True),
               HarvestRun.started_at >= cutoff)
        .distinct()
    ).all()
    return {account_key(p, u) for (p, u) in rows}


def coordinate_account_fails(rows, vouched: set[tuple[str, str]] | None = None
                             ) -> dict[tuple[str, str], int]:
    """Fold per-row fail counters into one number per PORTAL ACCOUNT.

    The worst row wins, so a genuinely bad or locked login stops every tenant
    sharing it instead of each burning its own MAX_LOGIN_FAILS attempts. But an
    account that can still be logged into is never dragged down by one tenant's
    local trouble — `vouched` (see `accounts_with_recent_fresh_login`) and a
    sibling sitting at a clean counter both clear it. Without that escape hatch,
    one credential row failing for days would take a 50%-healthy capture dark.
    """
    vouched = vouched or set()
    groups: dict[tuple[str, str], list] = {}
    for c in rows:
        groups.setdefault(account_key(c.provider, c.username_lc), []).append(c)
    out: dict[tuple[str, str], int] = {}
    for key, group in groups.items():
        ok_now = any((g.harvest_fails or 0) == 0 and g.last_harvest_ok is True
                     for g in group)
        out[key] = 0 if (ok_now or key in vouched) else max(
            (g.harvest_fails or 0) for g in group)
    return out


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
                    PortalCredential.last_harvest_ok,
                    PortalCredential.cloud_capture_enabled,
                )
            )
        ).scalars().all()
        # Lockout state is per PORTAL ACCOUNT, not per tenant row (see account_key).
        acct_fails = coordinate_account_fails(
            rows, accounts_with_recent_fresh_login(db))
        # Cache allow decisions per tenant_id (demo/allowlist still need Tenant).
        allowed_cache: dict[str, bool] = {}
        for c in rows:
            if not _is_due(c, _now, acct_fails.get(account_key(c.provider, c.username_lc))):
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
