"""Loud operator alerts for Cloud Capture: lockout pauses AND stalled captures.

The lockout pause (scheduler.MAX_LOGIN_FAILS) is the ONE legitimate back-off in
this codebase — a wrong/changed password or an MFA wall makes every login fail,
and hammering that is exactly how a portal's own lockout policy trips. But a
back-off that nobody is told about is the silent self-disarm Ford banned
(memory: no-self-sabotage-reliability-audit). Before this module a paused
credential simply stopped harvesting, forever, and the only way back was the
owner happening to re-save the password. A real SMA login sat paused in prod
with zero notification anywhere.

So: the pause now retries on a slow heartbeat (scheduler.PAUSED_RETRY) AND this
watchdog emails the operator for as long as it stays paused. It is a pure read
over the credential table + `InverterAlertState` dedup — it never mutates a
credential, never suppresses a signal, and re-alerts every `_REALERT_HOURS`
rather than once-ever.

The SECOND watchdog here exists because the lockout pause is deliberately narrow:
it only counts login failures where we actually submitted the password, since
that is the only thing a portal can lock us out for. A capture can therefore fail
forever WITHOUT ever tripping the pause — an SSO session that won't resume, a
scrape that 500s every cycle, a vendor outage. `run_capture_stall_watchdog`
covers exactly that gap: it ignores WHY and alerts on the only thing that matters
to the customer, "this login has not successfully captured anything in N hours".
Without it, narrowing the pause would have made a whole failure class quieter,
which is a regression, not a fix (memory: no-self-sabotage-reliability-audit).
This matters more than usual right now because `vip_watch` — the other
staleness net — was switched off on 2026-07-19.

Dedup keys are namespaced `cloud_capture_login_paused:<provider>:<username_lc>`
and `cloud_capture_stalled:<tenant>:<provider>:<username_lc>` — the pause is per
PORTAL ACCOUNT because that is what locks out (see scheduler.account_key), while
a stall is per credential row because one tenant can stall while its siblings
capture fine. Neither key contains "|", so the inverter sweep's reconcile leaves
them alone (memory: shared-alert-state-table).
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlalchemy import select

from ..db import SessionLocal
from ..models import HarvestRun, InverterAlertState, PortalCredential, Tenant, now
from ..notify import send_internal_alert
from .scheduler import (
    INVERTER_CODES,
    MAX_LOGIN_FAILS,
    PAUSED_RETRY,
    account_key,
    accounts_with_recent_fresh_login,
    coordinate_account_fails,
)

log = logging.getLogger("harvester.lockout")

KEY_PREFIX = "cloud_capture_login_paused:"
STALL_KEY_PREFIX = "cloud_capture_stalled:"
# Re-alert while a login is STILL paused. Frequent enough that a paused login
# can't rot for days unseen; deduped so it isn't per-tick spam.
_REALERT_HOURS = int(os.environ.get("CLOUD_CAPTURE_LOCKOUT_REALERT_HOURS") or 12)
# How long a credential may go without a SUCCESSFUL capture before it is a
# stall. Inverters are on a ~3-min loop with a 5-minute freshness promise, so
# hours of nothing is already far past broken; utility bills are monthly data on
# a ~12h cadence, so they get a day and a half before we shout.
STALL_INVERTER_HOURS = int(os.environ.get("CLOUD_CAPTURE_STALL_INVERTER_HOURS") or 3)
STALL_UTILITY_HOURS = int(os.environ.get("CLOUD_CAPTURE_STALL_UTILITY_HOURS") or 36)


def _incident_key(provider: str, username_lc: str) -> str:
    p, u = account_key(provider, username_lc)
    return f"{KEY_PREFIX}{p}:{u}"


def _enabled_credentials(db):
    """Every credential Cloud Capture is actually meant to be harvesting."""
    from sqlalchemy.orm import load_only

    return db.execute(
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
                PortalCredential.username,
                PortalCredential.username_lc,
                PortalCredential.harvest_fails,
                PortalCredential.last_harvest_at,
                PortalCredential.last_harvest_ok,
                PortalCredential.cloud_capture_enabled,
            )
        )
    ).scalars().all()


def paused_accounts() -> list[dict]:
    """Every portal account currently held at the lockout pause. Pure read."""
    out: list[dict] = []
    with SessionLocal() as db:
        rows = _enabled_credentials(db)
        # Same coordination the scheduler uses, so the alert can never disagree
        # with what is actually paused.
        acct_fails = coordinate_account_fails(
            rows, accounts_with_recent_fresh_login(db))
        groups: dict[tuple[str, str], list] = {}
        for c in rows:
            groups.setdefault(account_key(c.provider, c.username_lc), []).append(c)

        for key, group in groups.items():
            fails = max(acct_fails.get(key, 0),
                        max((g.harvest_fails or 0) for g in group))
            if fails < MAX_LOGIN_FAILS:
                continue
            provider, username_lc = key
            paused_rows = [g for g in group
                           if max((g.harvest_fails or 0), acct_fails.get(key, 0))
                           >= MAX_LOGIN_FAILS]
            last_run = db.execute(
                select(HarvestRun)
                .where(HarvestRun.provider == provider,
                       HarvestRun.username_lc == username_lc,
                       HarvestRun.status == "login_failed")
                .order_by(HarvestRun.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            last_at = max((g.last_harvest_at for g in paused_rows
                           if g.last_harvest_at is not None), default=None)
            out.append({
                "provider": provider,
                "username_lc": username_lc,
                "username": (paused_rows or group)[0].username,
                "fails": fails,
                "tenants": sorted(g.tenant_id for g in paused_rows),
                "tenants_sharing_account": len(group),
                "last_attempt_at": last_at,
                "last_error": (last_run.detail if last_run else None),
                "incident_key": _incident_key(provider, username_lc),
            })
    return out


def _stall_key(tenant_id: str, provider: str, username_lc: str) -> str:
    p, u = account_key(provider, username_lc)
    return f"{STALL_KEY_PREFIX}{tenant_id}:{p}:{u}"


def stalled_credentials() -> list[dict]:
    """Credentials that have not SUCCESSFULLY captured anything in far too long.

    Deliberately cause-blind. The lockout pause only sees failures that spent a
    real password attempt, so every other way a capture can die — an SSO session
    that won't resume, a scrape failing every cycle, a vendor outage, a wedged
    harvester — is invisible to it. This is the backstop that makes all of them
    loud. Pure read.
    """
    out: list[dict] = []
    with SessionLocal() as db:
        rows = _enabled_credentials(db)
        now_ = now()
        for c in rows:
            provider = (c.provider or "").lower()
            bar = timedelta(hours=(STALL_INVERTER_HOURS if provider in INVERTER_CODES
                                   else STALL_UTILITY_HOURS))
            last_ok = db.execute(
                select(HarvestRun.started_at)
                .where(HarvestRun.tenant_id == c.tenant_id,
                       HarvestRun.provider == c.provider,
                       HarvestRun.username_lc == c.username_lc,
                       HarvestRun.status == "ok")
                .order_by(HarvestRun.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_ok is not None and last_ok > now_ - bar:
                continue
            last_run = db.execute(
                select(HarvestRun)
                .where(HarvestRun.tenant_id == c.tenant_id,
                       HarvestRun.provider == c.provider,
                       HarvestRun.username_lc == c.username_lc)
                .order_by(HarvestRun.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_ok is None and last_run is None:
                continue        # never run at all — newly added, not a stall yet
            out.append({
                "tenant_id": c.tenant_id,
                "provider": c.provider,
                "username": c.username,
                "username_lc": c.username_lc,
                "last_ok_at": last_ok,
                "hours_bar": int(bar.total_seconds() // 3600),
                "last_status": (last_run.status if last_run else None),
                "last_error": (last_run.detail if last_run else None),
                "last_attempt_at": (last_run.started_at if last_run else c.last_harvest_at),
                "incident_key": _stall_key(c.tenant_id, c.provider, c.username_lc),
            })
    return out


def run_capture_stall_watchdog(dry_run: bool = False) -> dict:
    """Alert on any Cloud Capture login that has stopped producing data.

    Same dedup/re-alert/clear contract as the lockout watchdog. Read-only.
    """
    stalled = stalled_credentials()
    by_key = {s["incident_key"]: s for s in stalled}
    now_ = now()
    cutoff = now_ - timedelta(hours=_REALERT_HOURS)
    to_alert: list[dict] = []
    cleared = 0

    with SessionLocal() as db:
        for st in db.execute(select(InverterAlertState).where(
                InverterAlertState.incident_key.startswith(STALL_KEY_PREFIX))
        ).scalars().all():
            if st.incident_key not in by_key:
                if not dry_run:
                    db.delete(st)
                cleared += 1
        for key, s in by_key.items():
            st = db.execute(select(InverterAlertState).where(
                InverterAlertState.incident_key == key)).scalar_one_or_none()
            if st is not None and st.last_alerted_at is not None and st.last_alerted_at >= cutoff:
                continue
            to_alert.append(s)
            if not dry_run:
                if st is None:
                    st = InverterAlertState(tenant_id=s["tenant_id"], incident_key=key)
                    db.add(st)
                st.last_alerted_at = now_
        if not dry_run:
            db.commit()

    result = {"stalled": len(stalled), "cleared": cleared,
              "alerted": [s["incident_key"] for s in to_alert]}
    if not to_alert:
        log.info("capture stall watchdog: %d stalled, nothing new to alert "
                 "(cleared %d recovered)", len(stalled), cleared)
        return result

    lines = [
        f"⚠️ {len(to_alert)} Cloud Capture login(s) have stopped producing data.",
        "",
        "These are NOT necessarily bad passwords — the lockout guard only counts "
        "failures that actually spent a password attempt. Anything else that "
        "kills a capture (an SSO session that won't resume, a scrape failing "
        "every cycle, a vendor outage) lands here instead. The harvester is "
        "still retrying all of them.",
        "",
    ]
    for s in sorted(to_alert, key=lambda x: (x["last_ok_at"] is not None, x["last_ok_at"] or now_)):
        last = s["last_ok_at"].isoformat(sep=" ", timespec="seconds") if s["last_ok_at"] else "never"
        when = s["last_attempt_at"].isoformat(sep=" ", timespec="seconds") if s["last_attempt_at"] else "—"
        lines.append(f"  {s['provider']} / {s['username']} [{s['tenant_id']}]")
        lines.append(f"    last successful capture: {last} (bar: {s['hours_bar']}h)")
        lines.append(f"    last attempt:            {when} → {s['last_status'] or '—'}")
        lines.append(f"    last detail:             {s['last_error'] or '—'}")
        lines.append("")

    if not dry_run:
        send_internal_alert(
            f"Cloud Capture: {len(to_alert)} login(s) have stopped capturing",
            "\n".join(lines))
    log.warning("capture stall watchdog: alerted %d stalled login(s) (cleared %d)",
                len(to_alert), cleared)
    return result


def run_cloud_capture_watchdogs(dry_run: bool = False) -> dict:
    """Both Cloud Capture health watchdogs — the entry point the scheduler calls."""
    return {"lockout": run_login_lockout_watchdog(dry_run=dry_run),
            "stall": run_capture_stall_watchdog(dry_run=dry_run)}


def run_login_lockout_watchdog(dry_run: bool = False) -> dict:
    """Alert on every Cloud-Capture login held at the lockout pause.

    Newly-paused accounts alert immediately; still-paused ones re-alert every
    `_REALERT_HOURS`; recovered ones clear their incident so the next lockout is
    a fresh, loud alert. Returns {'paused', 'alerted', 'cleared'}.
    """
    paused = paused_accounts()
    by_key = {p["incident_key"]: p for p in paused}
    now_ = now()
    cutoff = now_ - timedelta(hours=_REALERT_HOURS)
    to_alert: list[dict] = []
    cleared = 0

    with SessionLocal() as db:
        open_states = db.execute(select(InverterAlertState).where(
            InverterAlertState.incident_key.startswith(KEY_PREFIX))).scalars().all()
        for st in open_states:
            if st.incident_key not in by_key:
                if not dry_run:
                    db.delete(st)
                cleared += 1
        for key, p in by_key.items():
            st = db.execute(select(InverterAlertState).where(
                InverterAlertState.incident_key == key)).scalar_one_or_none()
            if st is not None and st.last_alerted_at is not None and st.last_alerted_at >= cutoff:
                continue
            to_alert.append(p)
            if not dry_run:
                if st is None:
                    st = InverterAlertState(
                        tenant_id=(p["tenants"] or ["-"])[0], incident_key=key)
                    db.add(st)
                st.last_alerted_at = now_
        if not dry_run:
            db.commit()

    result = {"paused": len(paused), "cleared": cleared,
              "alerted": [p["incident_key"] for p in to_alert]}
    if not to_alert:
        log.info("login lockout watchdog: %d paused, nothing new to alert "
                 "(cleared %d recovered)", len(paused), cleared)
        return result

    retry_h = int(PAUSED_RETRY.total_seconds() // 3600) or 1
    lines = [
        f"⚠️ {len(to_alert)} Cloud Capture login(s) are PAUSED at the lockout guard "
        f"({MAX_LOGIN_FAILS} consecutive failed fresh logins).",
        "",
        "Harvesting for these accounts is throttled to a slow retry — it is NOT "
        f"stopped (retrying every ~{retry_h}h) — but their data is going stale and "
        "will keep going stale until the login works again. Check the password / MFA "
        "at the portal, or re-save the login in the Credential Vault to re-arm "
        "immediately.",
        "",
    ]
    for p in to_alert:
        when = p["last_attempt_at"].isoformat(sep=" ", timespec="seconds") if p["last_attempt_at"] else "—"
        shared = (f", shared by {p['tenants_sharing_account']} tenants"
                  if p["tenants_sharing_account"] > 1 else "")
        lines.append(f"  {p['provider']} / {p['username']} — {p['fails']} consecutive "
                     f"login failures{shared}")
        lines.append(f"    tenants paused: {', '.join(p['tenants']) or '—'}")
        lines.append(f"    last attempt:   {when}")
        lines.append(f"    last error:     {p['last_error'] or '—'}")
        lines.append("")

    if not dry_run:
        send_internal_alert(
            f"Cloud Capture: {len(to_alert)} login(s) paused at the lockout guard",
            "\n".join(lines))
    log.warning("login lockout watchdog: alerted %d paused login(s) (cleared %d)",
                len(to_alert), cleared)
    return result
