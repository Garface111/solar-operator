"""Loud operator alert for a Cloud-Capture login stuck at the lockout pause.

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

Dedup keys are namespaced `cloud_capture_login_paused:<provider>:<username_lc>`
— one incident per PORTAL ACCOUNT, not per tenant, because the account is what
locks out (see scheduler.account_key). The key contains no "|", so the inverter
sweep's reconcile leaves it alone (memory: shared-alert-state-table).
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlalchemy import select

from ..db import SessionLocal
from ..models import HarvestRun, InverterAlertState, PortalCredential, Tenant, now
from ..notify import send_internal_alert
from .scheduler import MAX_LOGIN_FAILS, PAUSED_RETRY, account_key, coordinate_account_fails

log = logging.getLogger("harvester.lockout")

KEY_PREFIX = "cloud_capture_login_paused:"
# Re-alert while a login is STILL paused. Frequent enough that a paused login
# can't rot for days unseen; deduped so it isn't per-tick spam.
_REALERT_HOURS = int(os.environ.get("CLOUD_CAPTURE_LOCKOUT_REALERT_HOURS") or 12)


def _incident_key(provider: str, username_lc: str) -> str:
    p, u = account_key(provider, username_lc)
    return f"{KEY_PREFIX}{p}:{u}"


def paused_accounts() -> list[dict]:
    """Every portal account currently held at the lockout pause. Pure read."""
    out: list[dict] = []
    with SessionLocal() as db:
        from sqlalchemy.orm import load_only

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
                    PortalCredential.username,
                    PortalCredential.username_lc,
                    PortalCredential.harvest_fails,
                    PortalCredential.last_harvest_at,
                    PortalCredential.last_harvest_ok,
                    PortalCredential.cloud_capture_enabled,
                )
            )
        ).scalars().all()
        acct_fails = coordinate_account_fails(rows)
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
