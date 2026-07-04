"""Weekly data-freshness scorecard — the number that replaces the vibe.

Ford (2026-07-04): "the difference between feeling 97% and knowing it."
Every Monday this computes, from data that already exists, how fresh the
product's data actually was — per tenant and fleet-wide — and emails the
result to the internal alert address. No new instrumentation, no writes:
a pure read over DailyGeneration, Inverter.source_last_data_at,
PortalLoginStatus, and the digest-hold flag.

Headline metric: 7-DAY DAILY-GENERATION COVERAGE — of the last 7 complete
days x active generation arrays, what % have a DailyGeneration row? That is
the honest "was the data there when the product needed it" number: bills,
invoices, digests, and trends all sit on those rows.

Secondary sections:
  * live-source freshness — % of inverters whose vendor-side
    source_last_data_at is within 26h (the extension-vendor staleness window)
  * utility login health — automated / pending / failing / disabled counts
    from PortalLoginStatus (the Portal access roster's raw material)
  * digest holds — tenants whose morning digest is currently held for
    all-stale data (each one is a customer seeing an honest gap)
  * extension liveness — tenants whose capture extension checked in <24h ago

Run manually any time:
    python -m api.jobs.freshness_scorecard
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import (
    Array,
    DailyGeneration,
    Inverter,
    PortalLoginStatus,
    Tenant,
    now,
)
from ..notify import send_internal_alert

log = logging.getLogger(__name__)

# Vendor-side data older than this is stale (matches the fleet tree's
# extension-vendor staleness window in api/inverter_fleet.py).
_SOURCE_FRESH_HOURS = 26.0
# A login whose last successful pull is within this window counts automated
# (matches api/portal_access.py's roster threshold).
_LOGIN_FRESH = timedelta(hours=48)
_EXT_ALIVE = timedelta(hours=24)
_WINDOW_DAYS = 7


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def build_scorecard(as_of: date | None = None) -> dict:
    """Compute the scorecard. Pure read — returns a dict, sends nothing."""
    today = as_of or now().date()
    win_start = today - timedelta(days=_WINDOW_DAYS)   # [start, today) = 7 complete days
    ts = now()

    with SessionLocal() as db:
        tenants = db.execute(
            select(Tenant).where(Tenant.active.is_(True))
        ).scalars().all()
        tenants = [t for t in tenants if not getattr(t, "is_demo", False)]
        tenant_ids = {t.id for t in tenants}

        # ── headline: 7-day DailyGeneration coverage per array ──────────────
        arrays = db.execute(
            select(Array).where(Array.deleted_at.is_(None), Array.excluded.is_(False))
        ).scalars().all()
        arrays = [a for a in arrays if a.tenant_id in tenant_ids]
        covered = {
            (r[0], r[1])
            for r in db.execute(
                select(DailyGeneration.array_id, func.count(func.distinct(DailyGeneration.day)))
                .where(DailyGeneration.day >= win_start, DailyGeneration.day < today)
                .group_by(DailyGeneration.array_id)
            ).all()
        }
        days_by_array = {aid: n for aid, n in covered}
        per_tenant: dict[str, dict] = {}
        slots_total = 0
        slots_covered = 0
        for a in arrays:
            n = min(days_by_array.get(a.id, 0), _WINDOW_DAYS)
            slots_total += _WINDOW_DAYS
            slots_covered += n
            row = per_tenant.setdefault(a.tenant_id, {"arrays": 0, "slots": 0, "covered": 0})
            row["arrays"] += 1
            row["slots"] += _WINDOW_DAYS
            row["covered"] += n

        # ── live-source freshness snapshot ───────────────────────────────────
        inv_rows = db.execute(
            select(Inverter.source_last_data_at, Array.tenant_id)
            .join(Array, Inverter.array_id == Array.id)
            .where(Array.deleted_at.is_(None))
        ).all()
        inv_rows = [r for r in inv_rows if r[1] in tenant_ids]
        live_total = 0
        live_fresh = 0
        for last, _tid in inv_rows:
            if last is None:
                continue                      # no live feed at all — not a live source
            live_total += 1
            if (ts - last) <= timedelta(hours=_SOURCE_FRESH_HOURS):
                live_fresh += 1

        # ── utility login health (Portal access raw material) ────────────────
        logins = db.execute(select(PortalLoginStatus)).scalars().all()
        logins = [l for l in logins if l.tenant_id in tenant_ids]
        login_counts = {"automated": 0, "pending": 0, "failing": 0, "disabled": 0}
        for l in logins:
            if l.paused or (l.fails or 0) >= 3:
                login_counts["failing"] += 1
            elif not l.enabled:
                login_counts["disabled"] += 1
            elif l.last_ok_at and (ts - l.last_ok_at) <= _LOGIN_FRESH:
                login_counts["automated"] += 1
            else:
                login_counts["pending"] += 1

        # ── digest holds + extension liveness ────────────────────────────────
        held = [t for t in tenants if getattr(t, "digest_hold_notified_at", None)]
        ext_seen = [t for t in tenants
                    if t.extension_heartbeat_at and (ts - t.extension_heartbeat_at) <= _EXT_ALIVE]
        ext_users = [t for t in tenants if t.extension_heartbeat_at is not None]

        tenant_names = {t.id: (t.company_name or t.name or t.id) for t in tenants}

    tenant_lines = sorted(
        (
            {
                "tenant": tenant_names.get(tid, tid),
                "arrays": row["arrays"],
                "coverage_pct": _pct(row["covered"], row["slots"]),
            }
            for tid, row in per_tenant.items()
        ),
        key=lambda r: r["coverage_pct"],
    )

    return {
        "window": f"{win_start.isoformat()} → {(today - timedelta(days=1)).isoformat()}",
        "headline_coverage_pct": _pct(slots_covered, slots_total),
        "arrays_total": len(arrays),
        "array_days_covered": slots_covered,
        "array_days_total": slots_total,
        "per_tenant": tenant_lines,
        "live_sources_total": live_total,
        "live_sources_fresh": live_fresh,
        "live_fresh_pct": _pct(live_fresh, live_total),
        "utility_logins": login_counts,
        "digest_holds": [tenant_names.get(t.id, t.id) for t in held],
        "extension_alive": len(ext_seen),
        "extension_users": len(ext_users),
    }


def _render_text(sc: dict) -> str:
    lines = [
        f"FRESHNESS SCORECARD — week {sc['window']}",
        "",
        f"HEADLINE: {sc['headline_coverage_pct']}% daily-generation coverage",
        f"  ({sc['array_days_covered']} of {sc['array_days_total']} array-days across "
        f"{sc['arrays_total']} active arrays had a DailyGeneration row)",
        "",
        "Per tenant (worst first):",
    ]
    for r in sc["per_tenant"] or [{"tenant": "(no arrays)", "arrays": 0, "coverage_pct": 0.0}]:
        lines.append(f"  {r['coverage_pct']:5.1f}%  {r['tenant']}  ({r['arrays']} arrays)")
    ul = sc["utility_logins"]
    lines += [
        "",
        f"Live sources fresh (<{int(_SOURCE_FRESH_HOURS)}h): "
        f"{sc['live_sources_fresh']}/{sc['live_sources_total']} ({sc['live_fresh_pct']}%)",
        f"Utility logins: {ul['automated']} automated · {ul['pending']} pending · "
        f"{ul['failing']} FAILING · {ul['disabled']} disabled",
        f"Extension checked in <24h: {sc['extension_alive']}/{sc['extension_users']} "
        "extension-using tenants",
    ]
    if sc["digest_holds"]:
        lines.append(f"DIGEST HELD (all-stale fleets): {', '.join(sc['digest_holds'])}")
    else:
        lines.append("Digest holds: none")
    lines += [
        "",
        "Reading guide: the headline is the honest 'was data there when the product",
        "needed it' number. FAILING logins and digest holds are this week's to-fix",
        "list; each one is a customer whose automation has stopped.",
    ]
    return "\n".join(lines)


def run_weekly_scorecard() -> dict:
    """Compute + email the scorecard to the internal alert address."""
    sc = build_scorecard()
    subject = f"Freshness scorecard: {sc['headline_coverage_pct']}% coverage ({sc['window']})"
    try:
        send_internal_alert(subject, _render_text(sc))
    except Exception:
        log.exception("freshness scorecard: send failed")
    return sc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(_render_text(build_scorecard()))
