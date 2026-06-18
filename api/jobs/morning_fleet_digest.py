"""Morning fleet-health digest (Array Operator).

A once-a-day, plain-language email that tells an array owner — at a glance, over
coffee — whether their whole solar fleet is healthy. It mirrors the structure of
generation_watchdog.py (scan_* / run_* + read-only + a small per-tenant batch),
but instead of an INTERNAL billing-safety alert it sends a TENANT-FACING email:
one digest per active Array Operator owner, rendered from the SAME truth the
dashboard shows (build_fleet_tree).

What's in the email:
  * a header (fleet/operator name + the date)
  * top-line KPIs (arrays, inverters, # needing attention, producing-now / asleep)
  * a HIGHLIGHTS block (best & worst performing arrays by recent daily kWh, any
    arrays carrying an alert called out in amber/red, any inverter flagged)
  * a clean per-array summary row (name, status dot, recent output)
  * a green "All systems healthy" banner when summary.attention == 0, or an
    amber/red attention callout when something needs a look.

HONESTY (CLAUDE.md): we never invent production numbers. If an array has no
recent daily reading, or the fleet is asleep at send time (sun down), we say so
in words — "—" / "asleep" / "no recent data" — rather than printing a fake kWh.

Read-only on the data side: it builds the tree (which persists daily history as
a normal side effect of a fleet read, exactly like the dashboard) and emails. It
never mutates owner layout or billing.
"""
from __future__ import annotations

import html as _html
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Tenant
from .. import inverter_fleet, notify, branding

log = logging.getLogger(__name__)

# Status dot colors — green (ok) / amber (warn) / red (critical), matching the
# fleet alert "level" produced by inverter_fleet._array_alert().
_LEVEL_COLOR = {"ok": "#16a34a", "warn": "#d97706", "critical": "#dc2626"}
_LEVEL_LABEL = {"ok": "Healthy", "warn": "Needs a look", "critical": "Attention"}

# Plain-language phrasing for a flagged inverter's status.
_STATUS_PHRASE = {
    "dead": "stopped producing",
    "fault": "reporting a fault",
    "comm_gap": "gone quiet (no recent data)",
    "underperforming": "underperforming vs its neighbors",
}


def _fleet_name(tenant) -> str:
    """The owner-facing fleet name: company, else operator, else tenant name."""
    return (
        getattr(tenant, "company_name", None)
        or getattr(tenant, "operator_name", None)
        or getattr(tenant, "name", None)
        or "Your fleet"
    )


def _recent_kwh(col: dict) -> float | None:
    """The array's most recent daily kWh reading, or None if it has no history.

    `daily` is ascending [{date, kwh}, ...]; the last point is the freshest day.
    Returns None (not 0.0) when there is genuinely no data, so callers can SAY
    "no recent data" instead of printing a fabricated zero.
    """
    daily = col.get("daily") or []
    for pt in reversed(daily):
        kwh = pt.get("kwh")
        if kwh is not None:
            try:
                return float(kwh)
            except (TypeError, ValueError):
                return None
    return None


def _recent_day(col: dict) -> str | None:
    """The date string of the array's most recent daily reading, or None."""
    daily = col.get("daily") or []
    for pt in reversed(daily):
        if pt.get("kwh") is not None:
            return pt.get("date")
    return None


def _fmt_kwh(kwh: float | None) -> str:
    """Honest kWh rendering: a real number, or an em-dash when there's no data."""
    if kwh is None:
        return "—"
    return f"{kwh:,.1f} kWh"


def _flagged_inverters(tree: dict) -> list[dict]:
    """Every inverter across the fleet whose status is not 'ok', with its array
    name attached, worst-first. Drives the 'inverter flagged for attention' lines."""
    out: list[dict] = []
    for col in tree.get("columns", []):
        for inv in col.get("inverters", []):
            st = inv.get("status") or "ok"
            if st != "ok":
                out.append({
                    "array_name": col.get("array_name", ""),
                    "name": inv.get("name") or inv.get("sn") or "Inverter",
                    "status": st,
                    "phrase": _STATUS_PHRASE.get(st, "needs a look"),
                    "rank": inverter_fleet._ALERT_PRIORITY.get(st, 0),
                })
    out.sort(key=lambda x: x["rank"], reverse=True)
    return out


def _ranked_arrays(tree: dict) -> list[dict]:
    """Arrays that have a real recent kWh reading, sorted best → worst by it.
    Arrays with no data are excluded (we never rank on an invented number)."""
    scored = []
    for col in tree.get("columns", []):
        kwh = _recent_kwh(col)
        if kwh is not None:
            scored.append({"col": col, "kwh": kwh})
    scored.sort(key=lambda x: x["kwh"], reverse=True)
    return scored


# ─────────────────────────────── rendering ───────────────────────────────────

def build_digest_html(tenant, tree: dict) -> str:
    """Render the morning fleet-health digest as a self-contained HTML email.

    Mobile-friendly, inline-CSS only, dark-on-light, no external assets. Pure
    function of (tenant, tree): no DB, no I/O — so it's trivially testable and
    safe to render a sample from a fake tree.
    """
    summary = tree.get("summary", {}) or {}
    arrays_total = int(summary.get("arrays_total", 0) or 0)
    inverters_total = int(summary.get("inverters_total", 0) or 0)
    attention = int(summary.get("attention", 0) or 0)
    is_daylight = bool(summary.get("is_daylight", False))

    fleet = _html.escape(_fleet_name(tenant))
    today = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")
    producing_label = "Producing now" if is_daylight else "Asleep (sun down)"
    producing_value = "Yes" if is_daylight else "Resting"

    # ── banner: green all-clear, or amber/red attention callout ──────────────
    if attention == 0:
        banner = (
            '<div style="background:#ecfdf5;border:1px solid #a7f3d0;'
            'border-radius:10px;padding:16px 20px;margin:0 0 22px;">'
            '<span style="font-size:18px;font-weight:700;color:#047857;">'
            '✓ All systems healthy</span>'
            '<div style="color:#065f46;font-size:14px;margin-top:4px;">'
            'Every inverter we can see is producing as expected this morning.</div>'
            '</div>'
        )
    else:
        # red if any array is critical, else amber.
        worst = "warn"
        for col in tree.get("columns", []):
            if (col.get("alert", {}) or {}).get("level") == "critical":
                worst = "critical"
                break
        bg, border, fg = (
            ("#fef2f2", "#fecaca", "#b91c1c") if worst == "critical"
            else ("#fffbeb", "#fde68a", "#b45309")
        )
        noun = "inverter needs" if attention == 1 else "inverters need"
        banner = (
            f'<div style="background:{bg};border:1px solid {border};'
            f'border-radius:10px;padding:16px 20px;margin:0 0 22px;">'
            f'<span style="font-size:18px;font-weight:700;color:{fg};">'
            f'⚠ {attention} {noun} attention</span>'
            f'<div style="color:{fg};font-size:14px;margin-top:4px;">'
            f'Details are in Highlights below — open Array Operator for the full picture.</div>'
            f'</div>'
        )

    # ── KPI tiles ────────────────────────────────────────────────────────────
    def _kpi(value: str, label: str, color: str = "#0f172a") -> str:
        return (
            '<td style="padding:6px;width:25%;vertical-align:top;">'
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
            'padding:14px 10px;text-align:center;">'
            f'<div style="font-size:24px;font-weight:700;color:{color};line-height:1;">{value}</div>'
            f'<div style="font-size:12px;color:#64748b;margin-top:6px;">{label}</div>'
            '</div></td>'
        )

    attn_color = "#16a34a" if attention == 0 else (
        "#dc2626" if any((c.get("alert", {}) or {}).get("level") == "critical"
                         for c in tree.get("columns", [])) else "#d97706"
    )
    kpis = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:separate;margin:0 0 22px;"><tr>'
        + _kpi(str(arrays_total), "Arrays")
        + _kpi(str(inverters_total), "Inverters")
        + _kpi(str(attention), "Need attention", attn_color)
        + _kpi(producing_value, producing_label)
        + '</tr></table>'
    )

    # ── highlights: best / worst array, alert callouts, flagged inverters ────
    highlight_rows: list[str] = []
    ranked = _ranked_arrays(tree)
    if ranked:
        best = ranked[0]
        highlight_rows.append(
            f'<li style="margin:6px 0;color:#065f46;">'
            f'<b>Top producer:</b> {_html.escape(best["col"].get("array_name", "Array"))}'
            f' — {_fmt_kwh(best["kwh"])} on its latest day.</li>'
        )
        if len(ranked) > 1:
            worst_a = ranked[-1]
            highlight_rows.append(
                f'<li style="margin:6px 0;color:#334155;">'
                f'<b>Lowest producer:</b> {_html.escape(worst_a["col"].get("array_name", "Array"))}'
                f' — {_fmt_kwh(worst_a["kwh"])} on its latest day.</li>'
            )
    else:
        why = "the fleet is asleep right now" if not is_daylight else "no recent daily data has landed yet"
        highlight_rows.append(
            f'<li style="margin:6px 0;color:#64748b;">'
            f'No recent production numbers to rank — {why}.</li>'
        )

    # arrays carrying an alert, called out in amber/red
    for col in tree.get("columns", []):
        alert = col.get("alert", {}) or {}
        level = alert.get("level", "ok")
        if level in ("warn", "critical"):
            color = _LEVEL_COLOR[level]
            headline = _html.escape(alert.get("headline") or _LEVEL_LABEL[level])
            highlight_rows.append(
                f'<li style="margin:6px 0;color:{color};">'
                f'<b>{_html.escape(col.get("array_name", "Array"))}:</b> {headline}'
                f' ({alert.get("count", 0)} affected).</li>'
            )

    # individual flagged inverters
    for fi in _flagged_inverters(tree)[:8]:
        color = _LEVEL_COLOR["critical"] if fi["rank"] >= 4 else _LEVEL_COLOR["warn"]
        highlight_rows.append(
            f'<li style="margin:6px 0;color:{color};">'
            f'<b>{_html.escape(fi["name"])}</b> '
            f'<span style="color:#64748b;">({_html.escape(fi["array_name"])})</span>'
            f' — {_html.escape(fi["phrase"])}.</li>'
        )

    highlights = (
        '<h2 style="font-size:15px;color:#0f172a;margin:0 0 8px;">Highlights</h2>'
        '<ul style="margin:0 0 22px;padding-left:20px;font-size:14px;line-height:1.5;">'
        + "".join(highlight_rows)
        + '</ul>'
    )

    # ── per-array summary rows (name, status dot, recent output) ─────────────
    arr_rows: list[str] = []
    for col in tree.get("columns", []):
        alert = col.get("alert", {}) or {}
        level = alert.get("level", "ok")
        dot = _LEVEL_COLOR.get(level, "#94a3b8")
        state = _LEVEL_LABEL.get(level, "Healthy")
        kwh = _recent_kwh(col)
        day = _recent_day(col)
        if kwh is None:
            output = "asleep" if not is_daylight else "no recent data"
        else:
            output = _fmt_kwh(kwh)
            if day:
                output += f' <span style="color:#94a3b8;">({_html.escape(str(day))})</span>'
        name = _html.escape(col.get("array_name", "Array"))
        invs = col.get("inverter_count", 0)
        arr_rows.append(
            '<tr>'
            '<td style="padding:10px 12px;border-bottom:1px solid #eef2f7;">'
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'background:{dot};margin-right:8px;"></span>'
            f'<b style="color:#0f172a;">{name}</b>'
            f'<div style="color:#94a3b8;font-size:12px;margin-left:18px;">{invs} inverter'
            f'{"s" if invs != 1 else ""} · {state}</div></td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #eef2f7;text-align:right;'
            f'color:#334155;font-size:14px;white-space:nowrap;">{output}</td>'
            '</tr>'
        )

    if arr_rows:
        per_array = (
            '<h2 style="font-size:15px;color:#0f172a;margin:0 0 8px;">Your arrays</h2>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;border:1px solid #eef2f7;border-radius:10px;'
            'overflow:hidden;margin:0 0 22px;">'
            + "".join(arr_rows)
            + '</table>'
        )
    else:
        per_array = (
            '<p style="color:#64748b;font-size:14px;margin:0 0 22px;">'
            'No arrays are connected yet — add one in Array Operator to start '
            'watching it here.</p>'
        )

    dash = "https://arrayoperator.com"
    # ── assemble ─────────────────────────────────────────────────────────────
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>Morning fleet health — {fleet}</title></head>'
        '<body style="margin:0;padding:0;background:#f1f5f9;">'
        '<div style="display:none;max-height:0;overflow:hidden;">'
        f'Your morning fleet health for {today}.</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f1f5f9;padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#ffffff;border-radius:14px;'
        'box-shadow:0 1px 3px rgba(15,23,42,.08);overflow:hidden;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        '<tr><td style="padding:24px 28px 8px;">'
        '<div style="font-size:13px;color:#059669;font-weight:600;letter-spacing:.3px;'
        'text-transform:uppercase;">Array Operator · Morning fleet health</div>'
        f'<h1 style="font-size:22px;color:#0f172a;margin:6px 0 2px;">{fleet}</h1>'
        f'<div style="color:#64748b;font-size:14px;">{today}</div>'
        '</td></tr>'
        '<tr><td style="padding:18px 28px 4px;">'
        + banner + kpis + highlights + per_array +
        '</td></tr>'
        '<tr><td style="padding:4px 28px 26px;">'
        f'<a href="{dash}" style="display:inline-block;background:#059669;color:#ffffff;'
        'text-decoration:none;font-weight:600;font-size:14px;padding:11px 20px;'
        'border-radius:8px;">Open Array Operator →</a>'
        '<p style="color:#94a3b8;font-size:12px;margin:20px 0 0;line-height:1.5;">'
        'You\'re getting this because morning fleet-health digests are on for your '
        'account. Production figures are the latest daily readings we have; an '
        'em-dash means no recent data, never an estimate. Manage or turn off these '
        'digests any time in Array Operator.</p>'
        '</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def build_digest_text(tenant, tree: dict) -> str:
    """A short plain-text fallback for the digest (mirrors the HTML's substance).
    Honest about missing data; never invents kWh."""
    summary = tree.get("summary", {}) or {}
    attention = int(summary.get("attention", 0) or 0)
    fleet = _fleet_name(tenant)
    today = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")
    is_daylight = bool(summary.get("is_daylight", False))

    lines = [f"Morning fleet health — {fleet}", today, ""]
    if attention == 0:
        lines.append("All systems healthy: every inverter we can see is producing as expected.")
    else:
        lines.append(f"{attention} inverter(s) need attention this morning.")
    lines += [
        "",
        f"Arrays: {summary.get('arrays_total', 0)}   "
        f"Inverters: {summary.get('inverters_total', 0)}   "
        f"Need attention: {attention}   "
        f"Producing now: {'yes' if is_daylight else 'asleep'}",
        "",
        "Arrays:",
    ]
    for col in tree.get("columns", []):
        alert = col.get("alert", {}) or {}
        state = _LEVEL_LABEL.get(alert.get("level", "ok"), "Healthy")
        kwh = _recent_kwh(col)
        out = (_fmt_kwh(kwh) if kwh is not None
               else ("asleep" if not is_daylight else "no recent data"))
        lines.append(f"  - {col.get('array_name', 'Array')}: {state}, {out}")
    for fi in _flagged_inverters(tree)[:8]:
        lines.append(f"  ! {fi['name']} ({fi['array_name']}): {fi['phrase']}")
    lines += ["", "Open Array Operator: https://arrayoperator.com"]
    return "\n".join(lines)


# ─────────────────────────────── send / batch ────────────────────────────────

def _subject(tenant, tree: dict) -> str:
    attention = int((tree.get("summary", {}) or {}).get("attention", 0) or 0)
    fleet = _fleet_name(tenant)
    if attention == 0:
        return f"☀️ {fleet}: all systems healthy this morning"
    noun = "inverter needs" if attention == 1 else "inverters need"
    return f"⚠️ {fleet}: {attention} {noun} attention"


def send_digest_for_tenant(db, tenant: Tenant) -> bool:
    """Build the tree, render, and email ONE tenant their morning digest.

    Sends to tenant.contact_email from the Array Operator From, via the SAME
    Resend path the rest of the app uses (notify._send_via_resend with
    product='array_operator'). Returns True if an email was sent.
    """
    to = (getattr(tenant, "contact_email", None) or "").strip()
    if not to:
        log.info("morning_digest: tenant %s has no contact_email — skipping", tenant.id)
        return False

    tree = inverter_fleet.build_fleet_tree(db, tenant)
    html = build_digest_html(tenant, tree)
    text = build_digest_text(tenant, tree)
    subject = _subject(tenant, tree)

    return notify._send_via_resend(
        to=to,
        subject=subject,
        html=html,
        text=text,
        # Use the canonical product-correct From (branding.from_address) so this
        # tracks Resend domain-verification centrally instead of hardcoding a
        # sender that could bounce if the domain status changes.
        from_addr=branding.from_address("array_operator"),
        product="array_operator",
    )


def run_morning_digest() -> dict:
    """Daily morning digest: email every active Array Operator owner a fleet-health
    summary. Per-tenant try/except so one bad fleet never kills the batch.

    Returns {'sent': [...], 'skipped': int, 'errors': [...]}.
    """
    sent: list[str] = []
    skipped = 0
    errors: list[str] = []

    with SessionLocal() as outer:
        tenant_ids = [
            t.id for t in outer.execute(
                select(Tenant).where(
                    Tenant.active.is_(True),
                    Tenant.product == "array_operator",
                )
            ).scalars().all()
        ]

    for tid in tenant_ids:
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, tid)
                if tenant is None:
                    skipped += 1
                    continue
                if send_digest_for_tenant(db, tenant):
                    sent.append(tid)
                else:
                    skipped += 1
        except Exception as exc:  # one bad fleet must not stall the rest
            errors.append(f"{tid}: {exc}")
            log.warning("morning_digest: tenant %s failed: %s", tid, exc)

    if errors:
        notify.send_internal_alert(
            f"Morning fleet digest: {len(errors)} tenant(s) failed",
            "Some morning digests could not be built/sent:\n" + "\n".join(errors),
        )
    log.info("morning_digest: sent=%d skipped=%d errors=%d",
             len(sent), skipped, len(errors))
    return {"sent": sent, "skipped": skipped, "errors": errors}
