"""Inverter down/underperformance email-alert sweep (Array Operator).

For every tenant with inverter_alerts_enabled, build their fleet tree, find
inverters that are DOWN (dead/fault/comm_gap) or UNDERPERFORMING below the
tenant's threshold, and email a digest — but only for NEW incidents, so we don't
re-spam the same dead inverter every tick.

Incident de-dup + the grace window are tracked in the InverterAlertState table
(api/models.py): we record when an inverter first looked bad (first_flagged_at)
and when we last emailed about it (last_alerted_at). We email once the problem
has persisted past the tenant's grace window, then stay quiet until it recovers
(which clears the row) and trips again.

Wire this to run on a schedule (Railway cron / scheduler tick):
    from api.inverter_alert_sweep import run_sweep
    run_sweep()
It is safe to call frequently; the grace window + de-dup keep emails rare.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import SessionLocal
from .models import Tenant, InverterAlertState
from . import inverter_fleet, notify
from .email_skin import render_email_skin, render_email_skin_text

logger = logging.getLogger(__name__)

# statuses that always count as "down" regardless of the % threshold
_DOWN = {"dead", "fault", "comm_gap"}


def _now() -> datetime:
    return datetime.utcnow()


def _flagged_inverters(tree: dict, threshold_pct: int) -> list[dict]:
    """Return the inverters worth alerting on: any DOWN inverter, plus
    underperformers whose peer_index is below threshold_pct/100."""
    thr = max(0.0, min(1.0, threshold_pct / 100.0))
    out = []
    for col in tree.get("columns", []):
        for inv in col.get("inverters", []):
            st = inv.get("status")
            if st in _DOWN:
                out.append({"col": col, "inv": inv, "reason": st})
            elif st == "underperforming":
                pi = inv.get("peer_index")
                if pi is not None and pi < thr:
                    out.append({"col": col, "inv": inv, "reason": "underperforming"})
    return out


def _incident_key(col: dict, inv: dict) -> str:
    return f"{col.get('array_id')}|{inv.get('inverter_id') or inv.get('name')}"


def _render_email(tenant: Tenant, items: list[dict]) -> tuple[str, str, str]:
    n = len(items)
    subject = (
        f"⚠️ {n} inverter{'s' if n != 1 else ''} need attention"
        if n != 1 else
        f"⚠️ An inverter needs attention — {items[0]['inv'].get('name')}"
    )
    labels = {
        "dead": "stopped producing",
        "fault": "reporting a fault",
        "comm_gap": "gone quiet (no data)",
        "underperforming": "underperforming vs its neighbors",
    }
    rows = []
    text_rows = []
    for it in items:
        inv, col = it["inv"], it["col"]
        name = inv.get("name", "Inverter")
        arr = col.get("array_name", "")
        what = labels.get(it["reason"], "needs a look")
        rows.append(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee">'
            f'<b>{name}</b><br><span style="color:#666;font-size:13px">{arr}</span></td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#b45309">{what}</td></tr>'
        )
        text_rows.append(f"- {name} ({arr}): {what}")
    intro = (
        f'<p style="margin:0 0 16px;color:#334155;">Array Operator spotted {n} '
        f'inverter{"s" if n != 1 else ""} that need{"s" if n == 1 else ""} a look:</p>'
    )
    table = (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;">' + "".join(rows) + '</table>'
    )
    html = render_email_skin(
        preheader=f"{n} inverter{'s' if n != 1 else ''} on your fleet need attention.",
        headline="Inverter alert",
        intro_line="A heads-up from your fleet watch.",
        body_html=intro + table,
        cta={"label": "Open Array Operator →", "url": "https://arrayoperator.com"},
        footer_line=("You set these alerts in Array Operator — adjust the threshold "
                     "or turn them off any time in the Alerts panel."),
        product="array_operator",
    )
    text = render_email_skin_text(
        headline="Inverter alert",
        intro_line="A heads-up from your fleet watch.",
        body_text=("Array Operator spotted these inverters that need attention:\n\n"
                   + "\n".join(text_rows)),
        cta={"label": "Open Array Operator", "url": "https://arrayoperator.com"},
        product="array_operator",
    )
    return subject, html, text


def sweep_tenant(db, tenant: Tenant) -> int:
    """Process one tenant. Returns the number of inverters emailed about."""
    if not getattr(tenant, "inverter_alerts_enabled", False):
        return 0
    to = getattr(tenant, "inverter_alert_email", None) or tenant.contact_email
    if not to:
        return 0
    grace_h = int(getattr(tenant, "inverter_alert_grace_hours", 12) or 0)
    threshold = int(getattr(tenant, "inverter_alert_threshold_pct", 50) or 50)

    try:
        tree = inverter_fleet.build_fleet_tree(db, tenant)
    except Exception:
        logger.exception("alert sweep: build_fleet_tree failed for %s", tenant.id)
        return 0

    flagged = _flagged_inverters(tree, threshold)
    flagged_keys = {_incident_key(it["col"], it["inv"]): it for it in flagged}

    # Load existing incident state for this tenant.
    states = {
        s.incident_key: s
        for s in db.execute(
            select(InverterAlertState).where(InverterAlertState.tenant_id == tenant.id)
        ).scalars()
    }
    now = _now()
    to_email: list[dict] = []

    # Open / update incidents for currently-flagged inverters.
    for key, it in flagged_keys.items():
        st = states.get(key)
        if st is None:
            st = InverterAlertState(
                tenant_id=tenant.id, incident_key=key,
                first_flagged_at=now, last_alerted_at=None,
            )
            db.add(st)
            states[key] = st
        # Email once the problem has outlived the grace window and we haven't yet.
        grace_passed = st.first_flagged_at <= now - timedelta(hours=grace_h)
        if grace_passed and st.last_alerted_at is None:
            to_email.append(it)
            st.last_alerted_at = now

    # Clear incidents that have recovered (no longer flagged).
    for key, st in list(states.items()):
        if key not in flagged_keys:
            db.delete(st)

    if to_email:
        subject, html, text = _render_email(tenant, to_email)
        try:
            notify._send_via_resend(
                to=to, subject=subject, html=html, text=text, product="array_operator"
            )
        except Exception:
            logger.exception("alert sweep: send failed for %s", tenant.id)
            # roll back the last_alerted_at stamps so we retry next tick
            for it in to_email:
                states[_incident_key(it["col"], it["inv"])].last_alerted_at = None

    db.commit()
    return len(to_email)


def run_sweep() -> dict:
    """Sweep every tenant with alerts enabled. Returns a small summary."""
    emailed = 0
    tenants = 0
    with SessionLocal() as db:
        rows = db.execute(
            select(Tenant).where(Tenant.inverter_alerts_enabled.is_(True))
        ).scalars().all()
        for t in rows:
            tenants += 1
            try:
                emailed += sweep_tenant(db, t)
            except Exception:
                logger.exception("alert sweep: tenant %s failed", t.id)
    return {"tenants_swept": tenants, "inverters_emailed": emailed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_sweep())
