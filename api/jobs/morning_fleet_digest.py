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
  * a HIGHLIGHTS block (best & worst arrays by their LAST FULL DAY's kWh — never
    today's still-accumulating partial, which would read like a live snapshot; any
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
from ..stripe_helpers import ao_gets_vendor_emails

log = logging.getLogger(__name__)

# Status dot colors — blue (ok) / amber (warn) / red (critical), matching the
# fleet alert "level" produced by inverter_fleet._array_alert().
_LEVEL_COLOR = {"ok": "#2563eb", "warn": "#d97706", "critical": "#dc2626"}
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


def _vendor_daily(col: dict) -> list:
    """The array's VENDOR (inverter-telemetry) daily series — NOT the GMP utility
    meter. This is an INVERTER-health digest (Bruce: "only show vendor data here"),
    so every kWh figure reads from vendor data only. Bonus: it sidesteps the GMP
    bill-prorate future-dated kWh that was leaking into vendor+GMP arrays. Falls
    back to the combined `daily` only when a tree carries no split (older builds)."""
    split = col.get("daily_split") or {}
    v = split.get("vendor")
    if v is not None:
        return v
    return col.get("daily") or []


def _local_today_iso() -> str:
    """Today's date (ET) as 'YYYY-MM-DD', matching the daily-series date strings."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _last_full_day_point(col: dict) -> dict | None:
    """The array's most recent COMPLETE-day vendor daily point ({date, kwh}).

    CRITICAL: the digest sends in the MORNING, when TODAY's kWh is still
    accumulating — a partial, near-live figure that reads like an instantaneous
    snapshot, not a day's production. So we skip today and report the last FULL
    day (normally yesterday) — the honest "production of the last day". Returns
    None (not 0.0) when there is genuinely no complete-day history yet.
    """
    today = _local_today_iso()
    pts = _vendor_daily(col)
    for pt in reversed(pts):                       # newest → oldest, skip today
        if pt.get("kwh") is None or str(pt.get("date")) == today:
            continue
        try:
            float(pt["kwh"])
            return pt
        except (TypeError, ValueError):
            continue
    # Edge case: the ONLY data is today (a brand-new connect). Rather than say
    # "no data", fall back to it — rare in the morning, and still honest via its date.
    for pt in reversed(pts):
        if pt.get("kwh") is not None:
            try:
                float(pt["kwh"])
                return pt
            except (TypeError, ValueError):
                continue
    return None


def _recent_kwh(col: dict) -> float | None:
    """The array's total production on its LAST COMPLETE DAY (not today's partial),
    or None when there's no full-day history. None (not 0.0) so callers can SAY
    "no recent data" rather than printing a fabricated zero."""
    p = _last_full_day_point(col)
    if p is None:
        return None
    try:
        return float(p["kwh"])
    except (TypeError, ValueError):
        return None


def _recent_day(col: dict) -> str | None:
    """The date of the array's last COMPLETE-day reading (the day _recent_kwh is for)."""
    p = _last_full_day_point(col)
    return p.get("date") if p else None


def _fmt_kwh(kwh: float | None) -> str:
    """Honest kWh rendering: a real number, or an em-dash when there's no data."""
    if kwh is None:
        return "—"
    return f"{kwh:,.1f} kWh"


def _vendor_columns(tree: dict) -> list[dict]:
    """Only the INVERTER (vendor) arrays — those with at least one inverter. The
    digest is an inverter-health report, so GMP-only utility-bill arrays (zero
    inverters) are excluded from the count, the list, and the highlights (Bruce:
    "I only have N arrays in my sandbox … only show vendor data here")."""
    return [c for c in tree.get("columns", []) if int(c.get("inverter_count") or 0) > 0]


def _array_has_flag(col: dict) -> bool:
    """True when this array contains at least one non-'ok' inverter."""
    return any((inv.get("status") or "ok") != "ok" for inv in col.get("inverters", []))


def _flagged_inverters(cols: list[dict]) -> list[dict]:
    """Every inverter across the given arrays whose status is not 'ok', with its
    array name attached, worst-first. Drives the 'inverter flagged' lines."""
    out: list[dict] = []
    for col in cols:
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


def _ranked_arrays(cols: list[dict]) -> list[dict]:
    """Arrays that have a real recent kWh reading, sorted best → worst by it.
    Arrays with no data are excluded (we never rank on an invented number)."""
    scored = []
    for col in cols:
        kwh = _recent_kwh(col)
        if kwh is not None:
            scored.append({"col": col, "kwh": kwh})
    scored.sort(key=lambda x: x["kwh"], reverse=True)
    return scored


# ─────────────────────────────── rendering ───────────────────────────────────

def build_digest_html(tenant, tree: dict) -> str:
    """Render the morning fleet-health digest as a self-contained HTML email.

    Array Operator DAY skin: a light, control-room "utility blue on cool slate"
    look matching the product's default theme (theme-day.css). Forces light mode
    (color-scheme: light only + explicit bgcolors) so dark-mode mail clients
    don't auto-invert it. Mobile-friendly, inline-CSS only, no external assets.
    Pure function of (tenant, tree): no DB, no I/O -- trivially testable.
    """
    # ---- Array Operator day palette (mirrors theme-day.css) -----------------
    PAGE, CARD, TILE = "#f6f8fb", "#ffffff", "#f8fafc"
    INK, MUTED, FAINT, BODY = "#0f172a", "#64748b", "#94a3b8", "#334155"
    LINE, LINE2 = "#e2e8f0", "#eef2f7"
    BLUE, BLUE_DEEP = "#2563eb", "#1d4ed8"
    BLUE_BG, BLUE_BORDER, BLUE_TEXT = "#eff6ff", "#bfdbfe", "#1e40af"

    summary = tree.get("summary", {}) or {}
    is_daylight = bool(summary.get("is_daylight", False))
    # VENDOR (inverter) arrays only — drop GMP-only utility-bill arrays so the
    # counts + list match what the owner sees in their inverter sandbox.
    cols = _vendor_columns(tree)
    arrays_total = len(cols)
    inverters_total = sum(int(c.get("inverter_count") or 0) for c in cols)
    attention = sum(int((c.get("alert") or {}).get("count") or 0) for c in cols)

    fleet = _html.escape(_fleet_name(tenant))
    # Snapshot timing — shown in Eastern (the fleets' region) so the reader knows
    # exactly when this fleet state was captured. Falls back to UTC if tz data is absent.
    try:
        from zoneinfo import ZoneInfo
        _now = datetime.now(ZoneInfo("America/New_York")); _tzlabel = "ET"
    except Exception:
        _now = datetime.now(timezone.utc); _tzlabel = "UTC"
    today = _now.strftime("%A, %B %-d, %Y")
    snapshot = _now.strftime("%-I:%M %p ") + _tzlabel

    has_critical = any(
        (c.get("alert", {}) or {}).get("level") == "critical"
        for c in cols
    )

    # ---- banner: blue all-clear, or amber/red attention callout -------------
    if attention == 0:
        banner = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 22px;"><tr>'
            f'<td bgcolor="{BLUE_BG}" style="background:{BLUE_BG};border:1px solid {BLUE_BORDER};'
            f'border-radius:12px;padding:15px 18px;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="vertical-align:middle;padding-right:13px;">'
            f'<div style="width:34px;height:34px;border-radius:50%;background:{BLUE};'
            f'color:#ffffff;font-size:18px;font-weight:700;text-align:center;'
            f'line-height:34px;">&#10003;</div></td>'
            f'<td style="vertical-align:middle;">'
            f'<div style="font-size:17px;font-weight:700;color:{BLUE_DEEP};line-height:1.2;">'
            f'All systems healthy</div>'
            f'<div style="color:{BLUE_TEXT};font-size:13px;margin-top:2px;">'
            f'Every inverter we can see is producing as expected this morning.</div>'
            f'</td></tr></table></td></tr></table>'
        )
    else:
        bg, border, fg = (
            ("#fef2f2", "#fecaca", "#b91c1c") if has_critical
            else ("#fffbeb", "#fde68a", "#b45309")
        )
        noun = "inverter needs" if attention == 1 else "inverters need"
        banner = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 22px;"><tr>'
            f'<td bgcolor="{bg}" style="background:{bg};border:1px solid {border};'
            f'border-radius:12px;padding:15px 18px;">'
            f'<div style="font-size:17px;font-weight:700;color:{fg};">'
            f'&#9888; {attention} {noun} attention</div>'
            f'<div style="color:{fg};font-size:13px;margin-top:3px;">'
            f'Details are in Highlights below — open Array Operator for the full picture.</div>'
            f'</td></tr></table>'
        )

    # ---- health hero (mirrors the dashboard's headline "% fleet healthy") ----
    # Big health number + meter + a compact stat strip, the way the in-app Fleet
    # health card reads (Bruce asked the email to look like the dashboard). Health
    # % = share of inverters NOT flagged; email-safe (tables, solid colors).
    flagged_n = len(_flagged_inverters(cols))
    health_pct = round(100 * (inverters_total - flagged_n) / inverters_total) if inverters_total else 100
    hero_color = BLUE if flagged_n == 0 else ("#dc2626" if has_critical else "#d97706")
    meter_fill = max(3, min(100, health_pct))
    look_color = BLUE if flagged_n == 0 else hero_color
    kpis = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{CARD}" '
        f'style="margin:0 0 18px;border:1px solid {LINE};border-radius:14px;'
        f'box-shadow:0 1px 3px rgba(15,23,42,.05);"><tr><td style="padding:20px 22px;">'
        # big % + label
        f'<div style="font-size:48px;font-weight:800;color:{hero_color};line-height:.95;'
        f'letter-spacing:-1.5px;">{health_pct}'
        f'<span style="font-size:22px;color:{FAINT};font-weight:700;letter-spacing:0;">%</span></div>'
        f'<div style="font-size:11px;letter-spacing:1.3px;text-transform:uppercase;color:{FAINT};'
        f'font-weight:700;margin:5px 0 0;">Fleet health</div>'
        f'<div style="font-size:12.5px;color:{MUTED};margin:3px 0 0;">'
        f'{inverters_total - flagged_n} of {inverters_total} inverters reporting normally</div>'
        # meter (nested-table fill for client-safe %-width)
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:14px 0 0;table-layout:fixed;"><tr>'
        f'<td bgcolor="{LINE2}" style="background:{LINE2};border-radius:99px;font-size:0;line-height:0;">'
        f'<table role="presentation" width="{meter_fill}%" cellpadding="0" cellspacing="0"><tr>'
        f'<td bgcolor="{hero_color}" style="background:{hero_color};height:8px;border-radius:99px;'
        f'font-size:0;line-height:8px;">&nbsp;</td></tr></table></td></tr></table>'
        # stat strip
        f'<div style="margin:14px 0 0;font-size:13.5px;color:{MUTED};">'
        f'<b style="color:{INK};">{arrays_total}</b> arrays'
        f'<span style="color:{LINE};"> &nbsp;&middot;&nbsp; </span>'
        f'<b style="color:{INK};">{inverters_total}</b> inverters'
        f'<span style="color:{LINE};"> &nbsp;&middot;&nbsp; </span>'
        f'<b style="color:{look_color};">{flagged_n}</b> need a look'
        f'</td></tr></table>'
    )

    # ---- highlights ---------------------------------------------------------
    # Bruce's rule: when inverters need attention, the digest is JUST the list of
    # exactly which ones — no top/lowest-producer chatter, no whole-fleet table.
    # When all-healthy, keep the richer producer highlights.
    highlight_rows: list[str] = []
    flagged = _flagged_inverters(cols)
    if attention > 0:
        for fi in flagged[:12]:
            color = _LEVEL_COLOR["critical"] if fi["rank"] >= 4 else _LEVEL_COLOR["warn"]
            highlight_rows.append(
                f'<li style="margin:6px 0;color:{color};">'
                f'<b>{_html.escape(fi["name"])}</b> '
                f'<span style="color:{MUTED};">({_html.escape(fi["array_name"])})</span>'
                f' — {_html.escape(fi["phrase"])}.</li>'
            )
        highlights_label = "Inverters needing attention"
    else:
        ranked = _ranked_arrays(cols)
        if ranked:
            best = ranked[0]
            highlight_rows.append(
                f'<li style="margin:6px 0;color:{BLUE_DEEP};">'
                f'<b>Top producer:</b> {_html.escape(best["col"].get("array_name", "Array"))}'
                f' — {_fmt_kwh(best["kwh"])} on its last full day.</li>'
            )
            if len(ranked) > 1:
                worst_a = ranked[-1]
                highlight_rows.append(
                    f'<li style="margin:6px 0;color:{BODY};">'
                    f'<b>Lowest producer:</b> {_html.escape(worst_a["col"].get("array_name", "Array"))}'
                    f' — {_fmt_kwh(worst_a["kwh"])} on its last full day.</li>'
                )
        else:
            why = "the fleet is asleep right now" if not is_daylight else "no recent daily data has landed yet"
            highlight_rows.append(
                f'<li style="margin:6px 0;color:{MUTED};">'
                f'No recent production numbers to rank — {why}.</li>'
            )
        highlights_label = "Highlights"

    highlights = (
        f'<div style="font-size:12px;color:{FAINT};margin:0 0 9px;text-transform:uppercase;'
        f'letter-spacing:.6px;font-weight:700;">{highlights_label}</div>'
        '<ul style="margin:0 0 22px;padding-left:20px;font-size:14px;line-height:1.55;">'
        + "".join(highlight_rows)
        + '</ul>'
    )

    # ---- per-array summary rows ---------------------------------------------
    # On an attention day, show ONLY the arrays that hold a flagged inverter (the
    # ones the flagged list points at) — not the whole fleet. All-healthy days
    # show every vendor array. Either way: vendor arrays only.
    table_cols = [c for c in cols if _array_has_flag(c)] if attention > 0 else cols
    table_label = "Arrays needing attention" if attention > 0 else "Your arrays"
    arr_rows: list[str] = []
    for col in table_cols:
        alert = col.get("alert", {}) or {}
        level = alert.get("level", "ok")
        dot = _LEVEL_COLOR.get(level, FAINT)
        state = _LEVEL_LABEL.get(level, "Healthy")
        kwh = _recent_kwh(col)
        day = _recent_day(col)
        if kwh is None:
            output = "asleep" if not is_daylight else "no recent data"
        else:
            output = _fmt_kwh(kwh)
            if day:
                output += f' <span style="color:{FAINT};">({_html.escape(str(day))})</span>'
        name = _html.escape(col.get("array_name", "Array"))
        invs = col.get("inverter_count", 0)
        arr_rows.append(
            '<tr>'
            f'<td style="padding:11px 14px;border-bottom:1px solid {LINE2};">'
            f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;'
            f'background:{dot};margin-right:9px;"></span>'
            f'<b style="color:{INK};">{name}</b>'
            f'<div style="color:{FAINT};font-size:12px;margin-left:18px;margin-top:2px;">{invs} inverter'
            f'{"s" if invs != 1 else ""} · {state}</div></td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid {LINE2};text-align:right;'
            f'color:{BODY};font-size:14px;white-space:nowrap;">{output}</td>'
            '</tr>'
        )

    if arr_rows:
        per_array = (
            f'<div style="font-size:12px;color:{FAINT};margin:0 0 3px;text-transform:uppercase;'
            f'letter-spacing:.6px;font-weight:700;">{table_label}</div>'
            f'<div style="font-size:11.5px;color:{FAINT};margin:0 0 9px;">'
            f'Output shown is each array&rsquo;s total production on its last full day.</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{CARD}" '
            f'style="border-collapse:collapse;border:1px solid {LINE2};border-radius:12px;'
            f'overflow:hidden;margin:0 0 22px;">'
            + "".join(arr_rows)
            + '</table>'
        )
    else:
        per_array = (
            f'<p style="color:{MUTED};font-size:14px;margin:0 0 22px;">'
            'No arrays are connected yet — add one in Array Operator to start '
            'watching it here.</p>'
        )

    # Ford: on an attention day the per-array table just repeats the "Inverters
    # needing attention" list above — drop it. (The "Your arrays" overview still
    # shows on all-healthy days.)
    if attention > 0:
        per_array = ""

    dash = "https://arrayoperator.com"
    # ---- assemble -----------------------------------------------------------
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<meta name="color-scheme" content="light only">'
        '<meta name="supported-color-schemes" content="light">'
        f'<title>Morning fleet health — {fleet}</title>'
        '<style>:root{color-scheme:light only;supported-color-schemes:light;}'
        'body,table,td{color-scheme:light only;}</style></head>'
        f'<body bgcolor="{PAGE}" style="margin:0;padding:0;background:{PAGE};color-scheme:light only;">'
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;">'
        f'Your morning fleet health for {today}.</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{PAGE}" '
        f'style="background:{PAGE};padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{CARD}" style="max-width:560px;background:{CARD};border-radius:16px;'
        f'border:1px solid {LINE};box-shadow:0 1px 3px rgba(15,23,42,.06);overflow:hidden;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        f'<tr><td style="height:4px;line-height:4px;font-size:0;background:{BLUE};'
        f'background:linear-gradient(90deg,{BLUE_DEEP},{BLUE},#3b82f6);">&nbsp;</td></tr>'
        f'<tr><td bgcolor="{CARD}" style="padding:24px 28px 8px;">'
        f'<div style="font-size:12px;color:{BLUE};font-weight:700;letter-spacing:.6px;'
        'text-transform:uppercase;">Array Operator · Morning fleet health</div>'
        f'<h1 style="font-size:23px;color:{INK};margin:7px 0 2px;font-weight:700;">{fleet}</h1>'
        f'<div style="color:{MUTED};font-size:14px;">{today} &middot; snapshot as of {snapshot}</div>'
        '</td></tr>'
        f'<tr><td bgcolor="{CARD}" style="padding:18px 28px 4px;">'
        + kpis + banner + highlights + per_array +
        '</td></tr>'
        f'<tr><td bgcolor="{CARD}" style="padding:4px 28px 26px;">'
        f'<a href="{dash}" style="display:inline-block;background:{BLUE};color:#ffffff;'
        'text-decoration:none;font-weight:600;font-size:14px;padding:12px 22px;'
        'border-radius:9px;">Open Array Operator →</a>'
        f'<p style="color:{FAINT};font-size:12px;margin:20px 0 0;line-height:1.5;">'
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
    is_daylight = bool(summary.get("is_daylight", False))
    fleet = _fleet_name(tenant)
    try:
        from zoneinfo import ZoneInfo
        _now = datetime.now(ZoneInfo("America/New_York")); _tzlabel = "ET"
    except Exception:
        _now = datetime.now(timezone.utc); _tzlabel = "UTC"
    today = _now.strftime("%A, %B %-d, %Y")
    snapshot = _now.strftime("%-I:%M %p ") + _tzlabel
    # Vendor (inverter) arrays only — mirrors the HTML.
    cols = _vendor_columns(tree)
    attention = sum(int((c.get("alert") or {}).get("count") or 0) for c in cols)

    lines = [f"Morning fleet health — {fleet}", f"{today} · snapshot as of {snapshot}", ""]
    if attention == 0:
        lines.append("All systems healthy: every inverter we can see is producing as expected.")
    else:
        lines.append(f"{attention} inverter(s) need attention this morning.")
    inv_total = sum(int(c.get('inverter_count') or 0) for c in cols)
    flagged_n = len(_flagged_inverters(cols))
    health_pct = round(100 * (inv_total - flagged_n) / inv_total) if inv_total else 100
    lines += [
        "",
        f"Fleet health: {health_pct}% ({inv_total - flagged_n} of {inv_total} inverters reporting normally)",
        f"Arrays: {len(cols)}   Inverters: {inv_total}   Need attention: {attention}",
        "",
    ]
    if attention > 0:
        # Just the flagged inverters — the per-array table is dropped (it repeats this).
        lines.append("Inverters needing attention:")
        for fi in _flagged_inverters(cols)[:12]:
            lines.append(f"  ! {fi['name']} ({fi['array_name']}): {fi['phrase']}")
    else:
        lines.append("Arrays (total production on their last full day):")
        for col in cols:
            alert = col.get("alert", {}) or {}
            state = _LEVEL_LABEL.get(alert.get("level", "ok"), "Healthy")
            kwh = _recent_kwh(col)
            out = (_fmt_kwh(kwh) if kwh is not None
                   else ("asleep" if not is_daylight else "no recent data"))
            lines.append(f"  - {col.get('array_name', 'Array')}: {state}, {out}")
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

    # Invoicing-only AO accounts bought offtaker invoicing, not fleet monitoring —
    # a vendor-data health digest is noise for them. (monitoring/both/no-plan still get it.)
    if not ao_gets_vendor_emails(getattr(tenant, "product", None),
                                 getattr(tenant, "billing_plan", None)):
        log.info("morning_digest: tenant %s is invoicing-only — skipping vendor digest", tenant.id)
        return False

    # Stable verdicts: judge health on COMPLETE days only (drop today's partial
    # dawn day) + cohort-relative "gone quiet", so morning fog/cloud variability
    # never produces a false "needs attention" digest (Bruce's noon-prior-day fix).
    tree = inverter_fleet.build_fleet_tree(db, tenant, stable_verdicts=True)
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
