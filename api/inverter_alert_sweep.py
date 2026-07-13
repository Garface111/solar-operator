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
from .stripe_helpers import ao_gets_vendor_emails

logger = logging.getLogger(__name__)

# statuses that always count as "down" regardless of the % threshold
_DOWN = {"dead", "fault", "comm_gap"}

# ── Live "dark right now" anomaly (mirrors the frontend isLiveAnomaly) ─────────
# An inverter the 14-day window still calls "ok" can have stopped producing in
# the last hour — a tripped breaker, a string fault, an inverter that hung at
# midday. We catch it FAST (within ~1h) instead of waiting up to 2 days for it
# to become "dead": if it's daylight, the array's telemetry is genuinely fresh,
# and this inverter reads ~0 W while >=2 of its siblings produce, it's dark.
LIVE_FLOOR_W = 25.0            # below this (or 1% of rated) = idle, not producing
LIVE_DARK_MAX_AGE_HOURS = 2.0  # only page on a dark reading from CURRENT data
LIVE_DARK_GRACE_HOURS = 1      # confirm across two hourly sweeps before paging


def _now() -> datetime:
    return datetime.utcnow()


def _live_floor_w(inv: dict) -> float:
    np = inv.get("nameplate_kw")
    if np:
        try:
            return max(LIVE_FLOOR_W, float(np) * 1000.0 * 0.01)
        except (TypeError, ValueError):
            pass
    return LIVE_FLOOR_W


def _is_producing(inv: dict) -> bool:
    p = inv.get("current_power_w")
    return p is not None and p > _live_floor_w(inv)


# A peer must be producing this fraction of its rated power before we'll use it as
# evidence that a dark sibling is faulted. At dawn/under fog the whole array sits
# at a few % of rated, where one inverter reading ~0 is weather, not a fault — so
# we only call "dark" when peers are GENUINELY up (the array is producing for real).
MEANINGFUL_FRAC = 0.15


def _meaningfully_producing(inv: dict) -> bool:
    p = inv.get("current_power_w")
    if p is None or p <= _live_floor_w(inv):
        return False
    np = inv.get("nameplate_kw")
    if np:
        try:
            return p >= float(np) * 1000.0 * MEANINGFUL_FRAC
        except (TypeError, ValueError):
            pass
    return True  # no nameplate to judge against — fall back to the idle floor above


def _has_fresh_real_reading(inv: dict) -> bool:
    """True when this inverter's live power is a GENUINE per-device reading we
    captured recently — the only basis on which "dark right now" or "producing
    peer" can be trusted.

    Two ways a reading is untrustworthy, both of which fabricated Bruce's Chester
    false alert (Jul-03, 6 healthy Fronius inverters paged as "dark"):
      • ESTIMATED — the value is a split of the site total filled in because this
        device gave no per-unit reading this capture (power_estimated). Extension
        captures are partial: a varying subset of inverters reports per-device
        each cycle, the rest get a fill. A fill must never count as a producing
        peer (fake evidence a sibling is faulted) nor be judged dark itself.
      • STALE — our capture of THIS device's power (power_age_hours) is older than
        the live window, so we don't actually know its state right now, even if a
        SIBLING captured fresh and made the ARRAY-level source look current. The
        array-level freshness gate keys off the FRESHEST inverter, so one fresh
        sibling was enough to open the gate on 6 stale ones — this closes that.

    Backward-compatible: when a tree carries no per-inverter provenance
    (power_age_hours absent — older callers/tests), fall through to True and let
    the array-level source_status gate decide, preserving prior behavior."""
    if inv.get("power_estimated"):
        return False
    age = inv.get("power_age_hours")
    if age is None:
        return True
    return age <= LIVE_DARK_MAX_AGE_HOURS


def _real_source_outage(col: dict) -> bool:
    """True when a comm_gap on this array is a REAL stop in reporting, not just
    our extension capture cadence. Extension-captured vendors (Fronius/SMA/Chint)
    only update when the owner's browser captures, so last_report is OUR capture
    time — a day-old capture marks every inverter 'comm_gap' though nothing is
    wrong. The fleet tree already encodes this honestly in source_status.state:
    'ok'/'stale' = a real source outage (alert), 'unpolled'/'none' = capture gap
    (suppress). This is what makes alerts-on-by-default safe."""
    return (col.get("source_status") or {}).get("state") in ("ok", "stale")


def _flagged_inverters(tree: dict, threshold_pct: int) -> list[dict]:
    """Return the inverters worth alerting on — but ONLY when the claim is
    backed by data we actually hold (Ford, 2026-07-04: never send a false
    report; no inverter-out email unless the data is fresh):

      • dead / fault / comm_gap — require a REAL source signal
        (source_status.state ok/stale). On an 'unpolled'/'none' array the
        status is frozen on an old capture: a "dead" inverter may have been
        fixed days ago, a "fault" may have cleared. Suppress until data flows.
      • underperforming — a claim about CURRENT relative output, so it needs
        genuinely fresh data: source_status.state == "ok" only.
    """
    thr = max(0.0, min(1.0, threshold_pct / 100.0))
    out = []
    for col in tree.get("columns", []):
        for inv in col.get("inverters", []):
            st = inv.get("status")
            if st in _DOWN:
                if not _real_source_outage(col):
                    continue  # capture-cadence gap: stale verdict, not fresh evidence
                out.append({"col": col, "inv": inv, "reason": st})
            elif st == "underperforming":
                if (col.get("source_status") or {}).get("state") != "ok":
                    continue  # no fresh data — can't honestly claim it lags NOW
                pi = inv.get("peer_index")
                if pi is not None and pi < thr:
                    out.append({"col": col, "inv": inv, "reason": "underperforming"})
    return out


def _live_dark_inverters(tree: dict) -> list[dict]:
    """Inverters that 14-day health calls 'ok' but are dark RIGHT NOW while their
    peers produce — caught from current telemetry so a fresh midday outage pages
    within ~1h instead of waiting for the slow window to call it dead. Mirrors the
    dashboard's isLiveAnomaly; gated on genuinely-fresh PER-DEVICE data so we never
    page on a stale snapshot or a partial capture's fill (see _has_fresh_real_reading)."""
    out = []
    for col in tree.get("columns", []):
        if col.get("is_daylight") is False:
            continue  # night: zero output is expected (Sleeping), not a fault
        src = col.get("source_status") or {}
        age = src.get("age_hours")
        # A "dark now" reading only means something if the array's telemetry is
        # current: SolarEdge polls live; an extension vendor must have captured
        # within the window. Stale data → we don't actually know it's dark now.
        if src.get("state") in (None, "none") or age is None or age > LIVE_DARK_MAX_AGE_HOURS:
            continue
        # Only compare inverters we ACTUALLY read per-device this cycle. A partial
        # Fronius capture reports some units per-device and fills the rest (stale
        # or site-split) — the fills are neither valid producing-peer evidence nor
        # judgeable as dark. Restricting to fresh real readings is what stops a
        # healthy-but-uncaptured inverter from being paged "dark" (Bruce, Jul-03).
        real = [i for i in col.get("inverters", []) if _has_fresh_real_reading(i)]
        if sum(1 for i in real if _meaningfully_producing(i)) < 2:
            continue  # array isn't genuinely up yet (dawn/fog) — don't call anything dark
        for inv in real:
            if (inv.get("status") == "ok"
                    and inv.get("current_power_w") is not None
                    and not _is_producing(inv)):
                out.append({"col": col, "inv": inv, "reason": "live_dark"})
    return out


# ── Live "low vs peers" underperformance (mirrors the dashboard's liveVerdict
# "low", Ford 2026-07-01). An inverter PRODUCING but sitting well below its
# siblings right now — a partial string loss, heavy soiling, or a derating
# inverter — that the 14-day window hasn't caught yet. Flagged when >15% below
# the peer MEDIAN output-per-nameplate, with the same freshness + genuinely-up
# guards as live_dark so weather never pages.
LIVE_LOW_DEFICIT = 0.15         # >15% below the peer median pct-of-max
LIVE_LOW_MIN_PEER_MED = 0.30    # only judge when the cohort is genuinely producing
LIVE_LOW_GRACE_HOURS = 2        # must persist across sweeps (not a passing cloud)


def _pct_of_max(inv: dict):
    """Current output as a fraction of rated nameplate, or None."""
    np = inv.get("nameplate_kw")
    p = inv.get("current_power_w")
    if p is None or not np:
        return None
    try:
        return float(p) / (float(np) * 1000.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _live_low_inverters(tree: dict) -> list[dict]:
    """Inverters that 14-day health calls 'ok' and ARE producing, but sit >15%
    below their siblings' output-per-nameplate right now. Mirrors the dashboard's
    liveVerdict 'low'; same freshness + genuinely-producing guards as live_dark
    (including per-device fresh/real gating) so a dawn/fog reading or a partial
    capture's fill never pages."""
    out = []
    for col in tree.get("columns", []):
        if col.get("is_daylight") is False:
            continue
        src = col.get("source_status") or {}
        age = src.get("age_hours")
        if src.get("state") in (None, "none") or age is None or age > LIVE_DARK_MAX_AGE_HOURS:
            continue
        # Only judge inverters we actually read per-device this cycle (see
        # _has_fresh_real_reading) — a site-split fill or a stale value is not a
        # trustworthy "low vs peers" signal and must not be a peer either.
        invs = [i for i in col.get("inverters", []) if _has_fresh_real_reading(i)]
        producers = [i for i in invs if _meaningfully_producing(i)]
        if len(producers) < 2:
            continue  # array not genuinely up (dawn/fog) — nothing to compare against
        for inv in invs:
            if inv.get("status") != "ok" or not _is_producing(inv):
                continue
            my = _pct_of_max(inv)
            if my is None:
                continue
            peer_pcts = sorted(
                v for v in (_pct_of_max(p) for p in producers if p is not inv)
                if v is not None)
            if len(peer_pcts) < 2:
                continue
            med = peer_pcts[len(peer_pcts) // 2]     # peer median pct-of-max
            if med < LIVE_LOW_MIN_PEER_MED:
                continue
            if my < med * (1 - LIVE_LOW_DEFICIT):
                out.append({"col": col, "inv": inv, "reason": "live_low",
                            "pct_of_max": round(my, 3), "peer_median": round(med, 3)})
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
        "live_dark": "dark right now while its neighbors are producing",
        "live_low": "producing well below its neighbors right now",
    }
    rows = []
    text_rows = []
    for it in items:
        inv, col = it["inv"], it["col"]
        name = inv.get("name", "Inverter")
        arr = col.get("array_name", "")
        what = labels.get(it["reason"], "needs a look")
        # For a live peer gap, name the numbers so the email is actionable.
        if it["reason"] == "live_low" and it.get("pct_of_max") is not None:
            what = (f"producing ~{round(it['pct_of_max'] * 100)}% of max vs its "
                    f"neighbors' ~{round(it['peer_median'] * 100)}% right now")
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
        footer_line=("Array Operator watches your fleet and emails you when an "
                     "inverter needs attention — adjust the threshold or turn these "
                     "off any time from the 🔔 Alerts button on your dashboard."),
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
    # Invoicing-only AO accounts bought offtaker invoicing, not fleet monitoring —
    # vendor-health alerts are noise for them. (monitoring/both/no-plan still alert.)
    if not ao_gets_vendor_emails(getattr(tenant, "product", None),
                                 getattr(tenant, "billing_plan", None)):
        return 0
    # Recipients: inverter_alert_email may hold a comma/semicolon list (team alerts);
    # _send_via_resend accepts a list. Fall back to the tenant contact email.
    _raw_to = getattr(tenant, "inverter_alert_email", None) or tenant.contact_email
    to = [e.strip() for e in str(_raw_to or "").replace(";", ",").split(",") if e.strip()]
    if not to:
        return 0
    grace_h = int(getattr(tenant, "inverter_alert_grace_hours", 12) or 0)
    threshold = int(getattr(tenant, "inverter_alert_threshold_pct", 50) or 50)

    try:
        # Stable verdicts (complete-days-only + cohort-relative comm-gap) so the
        # 14-day/down incidents never fire on dawn weather noise. The live_dark
        # path below still reads the live current_power_w for same-day outages.
        tree = inverter_fleet.build_fleet_tree(db, tenant, stable_verdicts=True)
    except Exception:
        logger.exception("alert sweep: build_fleet_tree failed for %s", tenant.id)
        return 0

    # 14-day/down incidents + fast live "dark now" anomalies. A given inverter
    # can't appear in both (live_dark requires status=="ok"); if a live_dark
    # later hardens into dead/comm_gap the incident_key is the same, so we never
    # re-page the same physical outage.
    flagged = (_flagged_inverters(tree, threshold)
               + _live_dark_inverters(tree)
               + _live_low_inverters(tree))
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
        # Email once the problem has outlived its grace window and we haven't yet.
        # Live "dark now" anomalies use a short fixed grace (confirm across two
        # hourly sweeps, ~1h) so a real midday outage pages fast; the slower
        # 14-day/down incidents use the tenant's configured grace (default 12h).
        if it["reason"] == "live_dark":
            this_grace = LIVE_DARK_GRACE_HOURS
        elif it["reason"] == "live_low":
            this_grace = LIVE_LOW_GRACE_HOURS
        else:
            this_grace = grace_h
        grace_passed = st.first_flagged_at <= now - timedelta(hours=this_grace)
        if grace_passed and st.last_alerted_at is None:
            to_email.append(it)
            st.last_alerted_at = now

    # Clear incidents that have recovered (no longer flagged).
    for key, st in list(states.items()):
        # This sweep ONLY owns inverter incidents, whose key is
        # "<array_id>|<inverter_id-or-name>" and always contains a "|". The same
        # table is shared by other alert jobs (coop_session_dead:<tenant>:<prov>,
        # gmp_token_final:<tenant>) whose keys have NO "|". Deleting those here as
        # "recovered" wiped their dedup every sweep, so the co-op-DEAD and
        # GMP-token FINAL WARNING alerts re-fired on a loop (operator spammed
        # hourly/daily). Never reconcile another job's rows.
        if "|" not in key:
            continue
        if key not in flagged_keys:
            db.delete(st)

    if to_email:
        # Owner opted to fold inverter alerts into the morning fleet digest
        # (fewer emails). We still stamp last_alerted_at so incidents are
        # tracked, but skip the separate Resend page — the daily digest already
        # surfaces arrays/inverters that need attention.
        if getattr(tenant, "inverter_alerts_via_digest", False):
            logger.info(
                "alert sweep: tenant %s via_digest — %d incident(s) held for morning digest",
                tenant.id, len(to_email),
            )
        else:
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
