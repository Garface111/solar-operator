"""Energy Agent ops tools: financial health, capture health, documents, underperformer trends.

Called from energy_agent._run_tool — keeps energy_agent.py thinner.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, desc

from .models import (
    AgentDocument,
    Array,
    HarvestRun,
    PortalCredential,
    PortalLoginStatus,
    RepairTicket,
    Tenant,
    now,
)

log = logging.getLogger("api.ea_ops_tools")

_LOSS_WINDOW_DAYS = 14
_ENERGY_RATE_FALLBACK = 0.21
_DOC_TYPES = frozenset({
    "warranty_claim", "diagnostic", "service_request", "note", "other",
})


def _iso(dt) -> str | None:
    if not dt:
        return None
    try:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    except Exception:
        return None


def _rate(tenant: Tenant) -> float:
    try:
        r = float(getattr(tenant, "default_net_rate_per_kwh", None) or 0)
    except (TypeError, ValueError):
        r = 0.0
    return r if r > 0 else _ENERGY_RATE_FALLBACK


# ── fleet_financial_health ───────────────────────────────────────────────────

def fleet_financial_health(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Fleet-wide money at risk: $/mo burn, per-array cumulative estimate, recoverable if fixed.

    Reuses the same recoverable math as Fleet Triage / investigate_attention.
    """
    args = args or {}
    from . import energy_agent as ea

    rate = _rate(tenant)
    try:
        columns, _summary = ea._fleet_tree_columns(db, tenant)
    except Exception as e:
        return {"ok": False, "error": f"fleet tree failed: {e}"}

    columns = list(columns or [])
    per_array = []
    total_usd_mo = 0.0
    total_lost_kwh_14d = 0.0
    priced_units = 0

    for col in columns:
        if not isinstance(col, dict):
            continue
        rec = ea._column_recoverable(col, rate)
        usd_mo = float(rec.get("est_loss_usd_month") or 0)
        lost_kwh = float(rec.get("est_lost_kwh_14d") or 0)
        total_usd_mo += usd_mo
        total_lost_kwh_14d += lost_kwh
        priced_units += len(rec.get("priced_inverters") or [])

        # Cumulative loss estimate while issues persist:
        # open repair ticket age × current monthly burn for that array, else 14d window loss.
        arr_id = col.get("array_id") or col.get("id")
        days_open = None
        ticket_rows = []
        if arr_id:
            try:
                open_tix = db.execute(
                    select(RepairTicket).where(
                        RepairTicket.tenant_id == tenant.id,
                        RepairTicket.array_id == int(arr_id),
                        RepairTicket.status.in_(
                            ("open", "waiting_reply", "scheduled", "in_progress")
                        ),
                    )
                ).scalars().all()
                for t in open_tix:
                    age_d = max(1.0, (now() - (t.opened_at or now())).total_seconds() / 86400.0)
                    days_open = max(days_open or 0, age_d)
                    ticket_rows.append({
                        "ticket_id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "fail_type": t.fail_type,
                        "days_open": round(age_d, 1),
                    })
            except Exception:
                pass

        daily_burn = usd_mo / 30.0
        if days_open and daily_burn > 0:
            cumulative = round(daily_burn * days_open, 2)
            cumulative_basis = f"open_ticket_days×daily_burn (~{days_open:.0f}d)"
        else:
            cumulative = round(lost_kwh * rate, 2)
            cumulative_basis = "14d_window_lost_kwh×rate"

        if usd_mo >= 0.5 or lost_kwh >= 1 or ticket_rows:
            per_array.append({
                "array_id": arr_id,
                "name": col.get("name") or col.get("array_name") or "array",
                "usd_per_month_at_risk": round(usd_mo, 2),
                "est_lost_kwh_14d": round(lost_kwh, 1),
                "cumulative_loss_usd_est": cumulative,
                "cumulative_basis": cumulative_basis,
                "recoverable_if_fixed_usd_month": round(usd_mo, 2),
                "priced_inverters": rec.get("priced_inverters") or [],
                "open_tickets": ticket_rows,
            })

    per_array.sort(key=lambda r: -(r.get("usd_per_month_at_risk") or 0))

    return {
        "ok": True,
        "rate_usd_per_kwh": rate,
        "window_days": _LOSS_WINDOW_DAYS,
        "total_usd_per_month_at_risk": round(total_usd_mo, 2),
        "total_est_lost_kwh_14d": round(total_lost_kwh_14d, 1),
        "total_recoverable_if_fixed_usd_month": round(total_usd_mo, 2),
        "priced_inverter_count": priced_units,
        "arrays_with_loss": per_array[:40],
        "note": (
            "Matches Fleet Triage Recoverable tile math (dead/fault/underperforming only; "
            "live dark/low and comm_gap not priced yet). Cumulative is an estimate from "
            "open-ticket age × burn rate when available, else the 14-day window."
        ),
    }


# ── capture_health_detail ────────────────────────────────────────────────────

def capture_health_detail(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Per-vendor capture status: last success, last error, why stale."""
    args = args or {}
    provider_filter = (args.get("provider") or args.get("vendor") or "").strip().lower() or None

    creds = list(db.execute(
        select(PortalCredential).where(PortalCredential.tenant_id == tenant.id)
    ).scalars().all())
    statuses = list(db.execute(
        select(PortalLoginStatus).where(PortalLoginStatus.tenant_id == tenant.id)
    ).scalars().all())
    status_by_key = {
        ((s.provider or "").lower(), (s.username_lc or "").lower()): s
        for s in statuses
    }

    vendors = []
    for c in creds:
        prov = (c.provider or "").strip().lower()
        if provider_filter and prov != provider_filter and provider_filter not in prov:
            continue
        uname_lc = (c.username_lc or c.username or "").lower()
        st = status_by_key.get((prov, uname_lc))

        # Latest harvest runs
        runs = list(db.execute(
            select(HarvestRun).where(
                HarvestRun.tenant_id == tenant.id,
                HarvestRun.provider == c.provider,
            ).order_by(desc(HarvestRun.started_at)).limit(5)
        ).scalars().all())
        last_run = runs[0] if runs else None
        last_fail = next((r for r in runs if (r.status or "") not in ("ok", "skipped")), None)
        last_ok_run = next((r for r in runs if r.status == "ok"), None)

        last_ok_at = None
        if last_ok_run and last_ok_run.ended_at:
            last_ok_at = last_ok_run.ended_at
        elif c.last_harvest_ok and c.last_harvest_at:
            last_ok_at = c.last_harvest_at
        elif st and st.last_ok_at:
            last_ok_at = st.last_ok_at

        age_h = None
        if last_ok_at:
            try:
                age_h = max(0.0, (now() - last_ok_at.replace(tzinfo=None)
                                  if getattr(last_ok_at, "tzinfo", None) else now() - last_ok_at
                                  ).total_seconds() / 3600.0)
            except Exception:
                age_h = None

        run_status = (last_run.status if last_run else None) or (
            "ok" if c.last_harvest_ok else ("failed" if c.last_harvest_ok is False else "unknown")
        )
        why = _explain_capture_failure(
            run_status=run_status,
            detail=(last_run.detail if last_run else None) or (last_fail.detail if last_fail else None),
            harvest_fails=c.harvest_fails or 0,
            paused=bool(st.paused) if st else False,
            enabled=bool(c.cloud_capture_enabled),
            has_secret=bool(c.secret_enc),
            age_h=age_h,
            device_mode=(getattr(tenant, "capture_mode", None) or "").lower() == "device",
        )

        vendors.append({
            "provider": c.provider,
            "username": c.username,
            "cloud_enabled": bool(c.cloud_capture_enabled),
            "has_password_stored": bool(c.secret_enc),
            "last_harvest_at": _iso(c.last_harvest_at),
            "last_harvest_ok": c.last_harvest_ok,
            "harvest_fails": c.harvest_fails or 0,
            "last_success_at": _iso(last_ok_at),
            "hours_since_success": round(age_h, 1) if age_h is not None else None,
            "device_status": {
                "enabled": bool(st.enabled) if st else None,
                "paused": bool(st.paused) if st else None,
                "fails": st.fails if st else None,
                "last_ok_at": _iso(st.last_ok_at) if st else None,
            } if st else None,
            "last_run": {
                "status": last_run.status if last_run else None,
                "started_at": _iso(last_run.started_at) if last_run else None,
                "ended_at": _iso(last_run.ended_at) if last_run else None,
                "detail": (last_run.detail or "")[:400] if last_run else None,
                "logged_in_fresh": last_run.logged_in_fresh if last_run else None,
                "rows_written": last_run.rows_written if last_run else None,
            } if last_run else None,
            "last_error": {
                "status": last_fail.status if last_fail else None,
                "at": _iso(last_fail.started_at) if last_fail else None,
                "detail": (last_fail.detail or "")[:400] if last_fail else None,
            } if last_fail else None,
            "diagnosis": why,
            "recent_runs": [
                {
                    "status": r.status,
                    "at": _iso(r.started_at),
                    "detail": (r.detail or "")[:160],
                    "rows": r.rows_written,
                }
                for r in runs
            ],
        })

    # Extension heartbeat
    ext_hb = getattr(tenant, "extension_heartbeat_at", None)
    capture_mode = getattr(tenant, "capture_mode", None)

    return {
        "ok": True,
        "capture_mode": capture_mode,
        "extension_heartbeat_at": _iso(ext_hb),
        "vendor_count": len(vendors),
        "vendors": vendors,
        "stale_or_failing": [v for v in vendors if (v.get("diagnosis") or {}).get("severity") in ("critical", "warning")],
        "note": (
            "login_failed = credential/MFA problem; scrape_failed = signed in but data pull hiccuped "
            "(not a password issue); paused device vault = auto-login gave up after fails."
        ),
    }


def _explain_capture_failure(
    *,
    run_status: str | None,
    detail: str | None,
    harvest_fails: int,
    paused: bool,
    enabled: bool,
    has_secret: bool,
    age_h: float | None,
    device_mode: bool,
) -> dict:
    st = (run_status or "unknown").lower()
    d = (detail or "").lower()
    reasons = []
    severity = "ok"

    if not enabled and not device_mode:
        reasons.append("cloud capture disabled for this login")
        severity = "info"
    if not has_secret and enabled:
        reasons.append("no password stored — re-save portal login on Account → Auto-refresh")
        severity = "critical"
    if paused:
        reasons.append("device auto-login paused after repeated failures (likely wrong password)")
        severity = "critical"
    if st == "login_failed":
        reasons.append("portal login failed — password changed, MFA wall, or account lockout")
        severity = "critical"
    elif st == "scrape_failed":
        reasons.append("login succeeded but data scrape failed — not a password problem; will retry")
        severity = "warning"
    elif st in ("error", "failed"):
        reasons.append(f"last run status={st}")
        severity = "warning"
    elif st == "no_creds":
        reasons.append("no credentials available for harvest")
        severity = "critical"
    elif st == "disabled":
        reasons.append("harvest disabled")
        severity = "info"
    if "rate" in d and "limit" in d:
        reasons.append("possible rate limit from vendor portal")
        severity = "warning"
    if "timeout" in d or "timed out" in d:
        reasons.append("portal timed out / offline")
        severity = "warning"
    if "mfa" in d or "2fa" in d or "two-factor" in d:
        reasons.append("MFA / 2FA blocking automated login")
        severity = "critical"
    if harvest_fails and harvest_fails >= 3:
        reasons.append(f"{harvest_fails} consecutive harvest failures")
        if severity == "ok":
            severity = "warning"
    if age_h is not None and age_h > 30:
        reasons.append(f"data ~{age_h:.0f}h old (stale)")
        if severity == "ok":
            severity = "warning"
    if not reasons:
        if st == "ok" or (age_h is not None and age_h < 6):
            reasons.append("healthy")
        else:
            reasons.append("no recent successful harvest recorded")
            severity = "info" if severity == "ok" else severity

    return {
        "severity": severity,
        "summary": "; ".join(reasons),
        "raw_status": st,
        "raw_detail": (detail or "")[:300] if detail else None,
    }


# ── underperformer_history / steady candidate ────────────────────────────────

def underperformer_history(db, tenant: Tenant, *, args: dict | None = None) -> dict:
    """Peer-index style timeline from daily kWh: steady-low vs recent drop.

    Flags steady_underperformer_candidate when a unit has been consistently low
    for many weeks (shading/orientation) vs a sudden cliff (real fault).
    """
    args = args or {}
    from . import energy_agent as ea

    inv_id = args.get("inverter_id")
    array_name = (args.get("array_name") or "").strip()
    window_days = int(args.get("window_days") or 180)
    window_days = max(30, min(365, window_days))

    try:
        columns, _summary = ea._fleet_tree_columns(db, tenant)
    except Exception as e:
        return {"ok": False, "error": f"fleet tree failed: {e}"}

    columns = list(columns or [])
    units = []
    for col in columns:
        name = col.get("name") or ""
        if array_name and array_name.lower() not in name.lower():
            continue
        for inv in (col.get("inverters") or []):
            if inv_id and int(inv.get("inverter_id") or inv.get("id") or 0) != int(inv_id):
                continue
            units.append((col, inv))

    if not units:
        return {"ok": True, "units": [], "note": "no matching inverters"}

    out = []
    for col, inv in units[:40]:
        daily = list(inv.get("daily") or [])
        # daily is typically last ~14-30d from fleet tree; for longer, pull InverterDaily
        series = _daily_series_for_inv(db, inv, window_days)
        if not series and daily:
            series = [
                {"date": d.get("date"), "kwh": float(d.get("kwh") or 0)}
                for d in daily if d.get("date")
            ]

        peers = [i for i in (col.get("inverters") or []) if i is not inv]
        analysis = _trend_analysis(series, inv, peers, window_days)
        analysis.update({
            "inverter_id": inv.get("inverter_id") or inv.get("id"),
            "name": inv.get("name") or inv.get("sn"),
            "array_name": col.get("name"),
            "array_id": col.get("array_id") or col.get("id"),
            "status_14d": inv.get("status"),
            "peer_index_14d": inv.get("peer_index"),
            "expected_low": bool(inv.get("expected_low")),
            "expected_low_reason": inv.get("expected_low_reason"),
            "expected_low_breach": bool(inv.get("expected_low_breach")),
        })
        out.append(analysis)

    candidates = [u for u in out if u.get("steady_underperformer_candidate")]
    recent_drops = [u for u in out if u.get("pattern") == "recent_drop"]

    return {
        "ok": True,
        "window_days": window_days,
        "units": out,
        "steady_underperformer_candidates": candidates,
        "recent_drop_units": recent_drops,
        "advice": (
            "steady_underperformer_candidate → ask owner about shading/trees/snow; "
            "if yes, mark_inverter_expected_low. recent_drop → treat as real fault / repair path."
        ),
    }


def _daily_series_for_inv(db, inv: dict, window_days: int) -> list[dict]:
    from .models import InverterDaily
    inv_id = inv.get("inverter_id") or inv.get("id")
    if not inv_id:
        return []
    try:
        since = (now().date() - timedelta(days=window_days))
        rows = db.execute(
            select(InverterDaily).where(
                InverterDaily.inverter_id == int(inv_id),
                InverterDaily.day >= since,
            ).order_by(InverterDaily.day.asc())
        ).scalars().all()
        return [
            {"date": r.day.isoformat() if hasattr(r.day, "isoformat") else str(r.day),
             "kwh": float(r.kwh or 0)}
            for r in rows
        ]
    except Exception as e:
        log.info("inverter daily load failed: %s", e)
        return []


def _trend_analysis(series: list[dict], inv: dict, peers: list[dict], window_days: int) -> dict:
    """Compare early vs late halves of production; flag steady-low vs cliff."""
    if len(series) < 14:
        return {
            "pattern": "insufficient_history",
            "steady_underperformer_candidate": False,
            "timeline_summary": f"only {len(series)} daily points",
            "timeline": series[-30:],
        }

    kwhs = [float(p.get("kwh") or 0) for p in series]
    n = len(kwhs)
    # Split: first 60% vs last 40% (or first half vs second half for short series)
    split = max(7, int(n * 0.55))
    early = kwhs[:split]
    late = kwhs[split:]
    early_avg = sum(early) / len(early) if early else 0
    late_avg = sum(late) / len(late) if late else 0

    # Relative to own nameplate CF if available
    np_kw = float(inv.get("nameplate_kw") or 0) or None
    # Peer median of late window from fleet 14d if present
    peer_index = inv.get("peer_index")
    try:
        pi = float(peer_index) if peer_index is not None else None
    except (TypeError, ValueError):
        pi = None

    # Drop ratio
    if early_avg > 0.5:
        late_vs_early = late_avg / early_avg
    else:
        late_vs_early = 1.0 if late_avg < 1 else 2.0

    # Steady-low: consistently underproducing, little change over time
    under_14d = (inv.get("status") or "") == "underperforming" or (pi is not None and pi < 0.85)
    steady = (
        under_14d
        and late_vs_early >= 0.75  # not a recent cliff
        and early_avg > 0
        and late_avg > 0
        and n >= 60  # ~2 months of history preferred; soft with 30d
    )
    # Soften: if we only have ~30d but variance is low and peer is low
    if under_14d and not steady and n >= 21 and late_vs_early >= 0.85 and pi is not None and pi < 0.85:
        # Check coefficient of variation of weekly averages
        weeks = []
        for i in range(0, n, 7):
            chunk = kwhs[i:i + 7]
            if chunk:
                weeks.append(sum(chunk) / len(chunk))
        if len(weeks) >= 3:
            mean_w = sum(weeks) / len(weeks)
            if mean_w > 0:
                var = sum((w - mean_w) ** 2 for w in weeks) / len(weeks)
                cv = (var ** 0.5) / mean_w
                if cv < 0.35:
                    steady = True

    recent_drop = under_14d and late_vs_early < 0.55 and early_avg > 1.0

    if inv.get("expected_low"):
        pattern = "marked_expected_low"
        steady = False  # already handled
    elif recent_drop:
        pattern = "recent_drop"
    elif steady:
        pattern = "steady_low"
    elif under_14d:
        pattern = "underperforming_unclear"
    else:
        pattern = "ok"

    # Compact weekly timeline for the agent
    weekly = []
    for i in range(0, n, 7):
        chunk = series[i:i + 7]
        if not chunk:
            continue
        avg = sum(float(p.get("kwh") or 0) for p in chunk) / len(chunk)
        weekly.append({
            "week_start": chunk[0].get("date"),
            "avg_kwh_day": round(avg, 2),
        })

    return {
        "pattern": pattern,
        "steady_underperformer_candidate": bool(steady and not inv.get("expected_low")),
        "early_avg_kwh_day": round(early_avg, 2),
        "late_avg_kwh_day": round(late_avg, 2),
        "late_vs_early_ratio": round(late_vs_early, 3),
        "days_of_history": n,
        "timeline_weekly": weekly[-26:],  # ~6 months
        "timeline_summary": (
            f"{n}d history; early avg {early_avg:.1f} kWh/d → late {late_avg:.1f} kWh/d "
            f"({late_vs_early:.0%} of early); pattern={pattern}"
        ),
    }


# ── save_document ────────────────────────────────────────────────────────────

def _docs_dir(tenant_id: str) -> Path:
    root = Path(os.getenv("STORAGE_DIR", "storage"))
    d = root / "agent_docs" / tenant_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_document(db, tenant: Tenant, *, args: dict) -> dict:
    """Persist a chat-produced artifact; optionally render PDF and attach to ticket."""
    content = (args.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}
    title = (args.get("title") or "").strip() or "Energy Agent document"
    doc_type = (args.get("type") or args.get("doc_type") or "note").strip().lower()
    if doc_type not in _DOC_TYPES:
        doc_type = "other"
    ticket_id = args.get("ticket_id")
    make_pdf = args.get("make_pdf", True)
    content_format = (args.get("format") or "markdown").strip().lower()
    if content_format not in ("markdown", "text", "html"):
        content_format = "markdown"

    if ticket_id is not None:
        t = db.get(RepairTicket, int(ticket_id))
        if t is None or t.tenant_id != tenant.id:
            return {"ok": False, "error": "ticket not found"}

    doc = AgentDocument(
        tenant_id=tenant.id,
        ticket_id=int(ticket_id) if ticket_id is not None else None,
        doc_type=doc_type,
        title=title[:240],
        content=content[:200_000],
        content_format=content_format,
        created_by="agent",
    )
    db.add(doc)
    db.flush()

    pdf_path = None
    pdf_error = None
    if make_pdf:
        try:
            pdf_path = _render_doc_pdf(tenant, doc)
            doc.pdf_path = pdf_path
        except Exception as e:
            pdf_error = str(e)[:200]
            log.warning("save_document pdf failed: %s", e)

    db.flush()
    return {
        "ok": True,
        "document": {
            "id": doc.id,
            "ticket_id": doc.ticket_id,
            "type": doc.doc_type,
            "title": doc.title,
            "content_format": doc.content_format,
            "content_chars": len(doc.content or ""),
            "pdf_path": doc.pdf_path,
            "pdf_error": pdf_error,
            "created_at": _iso(doc.created_at),
            "download_hint": (
                f"/v1/energy-agent/documents/{doc.id}" if doc.id else None
            ),
        },
        "message": (
            f"Saved “{title}” as document #{doc.id}"
            + (f" on ticket #{ticket_id}" if ticket_id else "")
            + (" (PDF ready)" if doc.pdf_path else (" (text only — PDF failed)" if pdf_error else ""))
        ),
    }


def _render_doc_pdf(tenant: Tenant, doc: AgentDocument) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted
    from reportlab.lib.enums import TA_LEFT

    out_dir = _docs_dir(tenant.id)
    fname = f"doc_{doc.id}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', doc.title)[:40]}.pdf"
    path = out_dir / fname

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle", parent=styles["Heading1"], fontSize=14, spaceAfter=8,
    )
    meta_style = ParagraphStyle(
        "DocMeta", parent=styles["Normal"], fontSize=9, textColor="#555555", spaceAfter=12,
    )
    body_style = ParagraphStyle(
        "DocBody", parent=styles["Normal"], fontSize=10, leading=14, alignment=TA_LEFT,
    )

    story = []
    story.append(Paragraph(_esc(doc.title), title_style))
    meta = (
        f"Type: {doc.doc_type} · Tenant: {getattr(tenant, 'company_name', None) or tenant.name or tenant.id}"
        + (f" · Ticket #{doc.ticket_id}" if doc.ticket_id else "")
        + f" · {now().strftime('%Y-%m-%d %H:%M')} UTC"
    )
    story.append(Paragraph(_esc(meta), meta_style))
    story.append(Spacer(1, 0.15 * inch))

    # Simple markdown-ish → paragraphs
    text = doc.content or ""
    if doc.content_format == "html":
        text = re.sub(r"<[^>]+>", " ", text)
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            block = block.lstrip("#").strip()
            story.append(Paragraph(_esc(block), styles["Heading2"]))
        elif block.startswith("```"):
            inner = re.sub(r"^```\w*\n?", "", block)
            inner = re.sub(r"\n?```$", "", inner)
            story.append(Preformatted(inner[:8000], styles["Code"]))
        else:
            # collapse single newlines within para
            para = " ".join(line.strip() for line in block.splitlines())
            story.append(Paragraph(_esc(para), body_style))
        story.append(Spacer(1, 0.08 * inch))

    doc_pdf = SimpleDocTemplate(
        str(path), pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    doc_pdf.build(story)
    return str(path)


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def get_document(db, tenant: Tenant, doc_id: int) -> dict:
    doc = db.get(AgentDocument, int(doc_id))
    if doc is None or doc.tenant_id != tenant.id:
        return {"ok": False, "error": "document not found"}
    return {
        "ok": True,
        "document": {
            "id": doc.id,
            "ticket_id": doc.ticket_id,
            "type": doc.doc_type,
            "title": doc.title,
            "content": doc.content,
            "content_format": doc.content_format,
            "pdf_path": doc.pdf_path,
            "created_at": _iso(doc.created_at),
        },
    }


def list_documents(db, tenant: Tenant, *, ticket_id: int | None = None, limit: int = 20) -> dict:
    q = select(AgentDocument).where(AgentDocument.tenant_id == tenant.id)
    if ticket_id is not None:
        q = q.where(AgentDocument.ticket_id == int(ticket_id))
    q = q.order_by(desc(AgentDocument.created_at)).limit(max(1, min(50, limit)))
    rows = list(db.execute(q).scalars().all())
    return {
        "ok": True,
        "documents": [
            {
                "id": d.id,
                "ticket_id": d.ticket_id,
                "type": d.doc_type,
                "title": d.title,
                "pdf_path": d.pdf_path,
                "created_at": _iso(d.created_at),
                "content_chars": len(d.content or ""),
            }
            for d in rows
        ],
        "count": len(rows),
    }
