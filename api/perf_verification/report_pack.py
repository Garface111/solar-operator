"""Monthly performance verification report pack (PDF + HTML email).

Array Operator owners only. Never invents numbers — all figures come from a
portfolio/month verification snapshot built by engine.build_*_verification.
"""
from __future__ import annotations

import base64
import html as _html
import io
import logging
from typing import Any

from .standards import IEC_ALIGNMENT_NOTE, REPORT_FOOTER

log = logging.getLogger("perf_verification.report_pack")


def _fmt_pi(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.0f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_priority(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "—"


def _period_label(snapshot: dict) -> str:
    period = snapshot.get("period")
    if period:
        return str(period)
    start = snapshot.get("period_start") or snapshot.get("window_start") or ""
    end = snapshot.get("period_end") or snapshot.get("window_end") or ""
    if start and end:
        return f"{start} → {end}"
    return "verification window"


def _fleet_name(tenant) -> str:
    for attr in ("company_name", "operator_name", "name"):
        val = (getattr(tenant, attr, None) or "").strip()
        if not val:
            continue
        local = (getattr(tenant, "contact_email", None) or "").split("@")[0].strip()
        if local and val.lower() == local.lower():
            continue
        return val
    return "Your fleet"


def _assumptions_blurb(snapshot: dict) -> str:
    """Short plain-language assumptions from snapshot method fields only."""
    method = snapshot.get("method") or {}
    parts: list[str] = []
    meas = method.get("measurement_boundary")
    if meas:
        parts.append(str(meas))
    exp = method.get("expected_energy")
    if exp:
        parts.append(str(exp))
    note = snapshot.get("standards_note") or IEC_ALIGNMENT_NOTE
    if note and note not in parts:
        parts.append(str(note))
    if not parts:
        parts.append(IEC_ALIGNMENT_NOTE)
    thr = snapshot.get("threshold")
    if thr is not None:
        try:
            parts.append(f"Deviation threshold: {float(thr) * 100:.0f}% residual.")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


def render_verification_pdf(snapshot: dict) -> bytes:
    """Build a day-palette PDF from a portfolio/month verification snapshot.

    Honest empty state when snapshot available=False. Never fabricates PI.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("reportlab is required for verification PDFs") from e

    from ..billing import _pdf_brand as brand

    HERO_H = 1.45 * inch
    INK = colors.HexColor(brand.DAY_INK)
    MUTED = colors.HexColor(brand.DAY_MUTED)
    BLUE = colors.HexColor(brand.DAY_BLUE)
    GREEN = colors.HexColor(brand.DAY_GREEN)
    LINE = colors.HexColor(brand.DAY_LINE)
    PANEL = colors.HexColor(brand.DAY_PANEL)

    portfolio = snapshot.get("portfolio") or {}
    available = bool(snapshot.get("available"))
    period = _period_label(snapshot)
    pi = portfolio.get("performance_index") if available else None
    right_val = _fmt_pi(pi) if pi is not None else "—"
    right_label = "PORTFOLIO PI" if available else "NO DATA"

    decorate = brand.make_hero_decorator(
        title="Performance Verification",
        subtitle=period,
        right_label=right_label,
        right_value=right_val,
        footer_left="Array Operator  ·  arrayoperator.com",
        footer_right=(snapshot.get("report_footer") or REPORT_FOOTER)[:90],
        hero_h=HERO_H,
        light=True,
    )

    styles = getSampleStyleSheet()
    h2 = ParagraphStyle(
        "pv_h2", parent=styles["Heading2"], fontSize=12,
        textColor=INK, spaceBefore=10, spaceAfter=6, fontName="Helvetica-Bold",
    )
    body = ParagraphStyle(
        "pv_body", parent=styles["Normal"], fontSize=9,
        textColor=INK, leading=12,
    )
    small = ParagraphStyle(
        "pv_small", parent=styles["Normal"], fontSize=8,
        textColor=MUTED, leading=10,
    )
    cell = ParagraphStyle(
        "pv_cell", parent=styles["Normal"], fontSize=8,
        textColor=INK, leading=10,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=HERO_H + 0.3 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        title=f"Performance Verification — {period}",
    )
    story: list = []

    if not available:
        reason = snapshot.get("reason") or (
            "No arrays with matched measured vs expected energy in this window."
        )
        skipped = snapshot.get("skipped") or []
        story.append(Paragraph("No verification data this period", h2))
        story.append(Paragraph(_html.escape(str(reason)), body))
        if skipped:
            story.append(Spacer(1, 8))
            story.append(Paragraph(
                f"{len(skipped)} array(s) skipped "
                "(no nameplate, location, or measured days).",
                small,
            ))
        story.append(Spacer(1, 14))
        story.append(Paragraph(_html.escape(_assumptions_blurb(snapshot)), small))
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            _html.escape(snapshot.get("report_footer") or REPORT_FOOTER), small
        ))
        doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
        return buf.getvalue()

    # Portfolio KPIs
    story.append(Paragraph("Portfolio summary", h2))
    act = portfolio.get("actual_kwh")
    exp = portfolio.get("expected_matched_kwh")
    kpi_rows = [
        ["Performance index (PI)", _fmt_pi(portfolio.get("performance_index"))],
        ["Ratio (measured / expected)", _fmt_pct(portfolio.get("ratio_pct"))],
        [
            "Measured energy",
            f"{act:,.1f} kWh" if act is not None else "—",
        ],
        [
            "Expected (matched days)",
            f"{exp:,.1f} kWh" if exp is not None else "—",
        ],
        [
            "Arrays verified",
            str(portfolio.get("array_count")
                if portfolio.get("array_count") is not None else "—"),
        ],
        [
            "Arrays skipped",
            str(portfolio.get("skipped_count")
                if portfolio.get("skipped_count") is not None else "—"),
        ],
        ["Max priority", _fmt_priority(portfolio.get("max_priority"))],
    ]
    kt = Table(kpi_rows, colWidths=[2.6 * inch, 4.0 * inch])
    kt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, 0), (1, 0), GREEN),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
    ]))
    story.append(kt)
    story.append(Spacer(1, 12))

    # Per-array table
    story.append(Paragraph("Per-array verification", h2))
    headers = ["Array", "PI", "Ratio", "Boundary", "Deviation", "Priority", "Cause"]
    rows: list = [headers]
    for arr in snapshot.get("arrays") or []:
        dev = arr.get("deviation") or {}
        cause = arr.get("cause") or {}
        rows.append([
            Paragraph(_html.escape(str(arr.get("array_name") or "—")[:40]), cell),
            _fmt_pi(arr.get("performance_index")),
            _fmt_pct(arr.get("ratio_pct")),
            str(arr.get("boundary") or "—"),
            str(dev.get("label") or "—"),
            _fmt_priority(dev.get("priority")),
            str(cause.get("cause") or "—"),
        ])

    if len(rows) == 1:
        story.append(Paragraph("No arrays with available verification data.", body))
    else:
        col_w = [
            1.6 * inch, 0.7 * inch, 0.7 * inch, 0.9 * inch,
            0.9 * inch, 0.7 * inch, 0.9 * inch,
        ]
        at = Table(rows, colWidths=col_w, repeatRows=1)
        at.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("GRID", (0, 0), (-1, -1), 0.4, LINE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ]))
        story.append(at)

    story.append(Spacer(1, 14))
    story.append(Paragraph("Assumptions & method", h2))
    story.append(Paragraph(_html.escape(_assumptions_blurb(snapshot)), small))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        _html.escape(snapshot.get("report_footer") or REPORT_FOOTER), small
    ))

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


# Alias used by an earlier partial implementation / job drafts
render_verification_pdf_bytes = render_verification_pdf


def render_verification_html(
    snapshot: dict, *, company_name: str
) -> tuple[str, str]:
    """HTML + plain-text email body for a verification snapshot.

    Returns (html_body, text_body) — unwrapped body fragments; callers wrap
    with email_skin as needed.
    """
    period = _period_label(snapshot)
    company = (company_name or "Your fleet").strip() or "Your fleet"
    available = bool(snapshot.get("available"))
    portfolio = snapshot.get("portfolio") or {}
    footer = snapshot.get("report_footer") or REPORT_FOOTER

    if not available:
        reason = snapshot.get("reason") or (
            "No arrays had matched measured vs expected energy this period."
        )
        html = (
            f'<div style="font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;'
            f'color:#0f172a">'
            f'<p><strong>{_html.escape(company)}</strong> — Performance Verification '
            f'for <strong>{_html.escape(period)}</strong></p>'
            f'<p style="color:#64748b">No verification data this period.</p>'
            f'<p>{_html.escape(str(reason))}</p>'
            f'<p style="font-size:12px;color:#94a3b8;margin-top:24px">'
            f'{_html.escape(footer)}</p></div>'
        )
        text = (
            f"{company} — Performance Verification for {period}\n\n"
            f"No verification data this period.\n{reason}\n\n{footer}\n"
        )
        return html, text

    pi = _fmt_pi(portfolio.get("performance_index"))
    ratio = _fmt_pct(portfolio.get("ratio_pct"))
    act = portfolio.get("actual_kwh")
    exp = portfolio.get("expected_matched_kwh")
    act_s = f"{act:,.1f} kWh" if act is not None else "—"
    exp_s = f"{exp:,.1f} kWh" if exp is not None else "—"
    n = portfolio.get("array_count")
    skip = portfolio.get("skipped_count")

    rows_html = []
    rows_text = []
    for arr in snapshot.get("arrays") or []:
        dev = arr.get("deviation") or {}
        cause = arr.get("cause") or {}
        name = str(arr.get("array_name") or "—")
        a_pi = _fmt_pi(arr.get("performance_index"))
        a_ratio = _fmt_pct(arr.get("ratio_pct"))
        boundary = str(arr.get("boundary") or "—")
        label = str(dev.get("label") or "—")
        pri = _fmt_priority(dev.get("priority"))
        c = str(cause.get("cause") or "—")
        rows_html.append(
            "<tr>"
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">'
            f"{_html.escape(name)}</td>"
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;'
            f'text-align:right">{_html.escape(a_pi)}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;'
            f'text-align:right">{_html.escape(a_ratio)}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">'
            f"{_html.escape(boundary)}</td>"
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">'
            f"{_html.escape(label)}</td>"
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;'
            f'text-align:right">{_html.escape(pri)}</td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">'
            f"{_html.escape(c)}</td>"
            "</tr>"
        )
        rows_text.append(
            f"  · {name}: PI {a_pi}, ratio {a_ratio}, {boundary}, "
            f"{label}, priority {pri}, cause {c}"
        )

    table = (
        '<table style="width:100%;border-collapse:collapse;font-size:13px;'
        'margin:12px 0">'
        '<thead><tr style="background:#2563eb;color:#fff">'
        '<th style="text-align:left;padding:6px 8px">Array</th>'
        '<th style="text-align:right;padding:6px 8px">PI</th>'
        '<th style="text-align:right;padding:6px 8px">Ratio</th>'
        '<th style="text-align:left;padding:6px 8px">Boundary</th>'
        '<th style="text-align:left;padding:6px 8px">Deviation</th>'
        '<th style="text-align:right;padding:6px 8px">Priority</th>'
        '<th style="text-align:left;padding:6px 8px">Cause</th>'
        "</tr></thead><tbody>"
        + (
            "".join(rows_html)
            or '<tr><td colspan="7" style="padding:8px">No arrays</td></tr>'
        )
        + "</tbody></table>"
    )

    skip_bit = f" · {skip} skipped" if skip else ""
    html = (
        f'<div style="font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;'
        f'color:#0f172a">'
        f'<p><strong>{_html.escape(company)}</strong> — Performance Verification '
        f'for <strong>{_html.escape(period)}</strong></p>'
        f'<p style="margin:8px 0 4px"><span style="color:#64748b">Portfolio PI</span> '
        f'<strong style="color:#047857;font-size:22px">{_html.escape(pi)}</strong> '
        f'<span style="color:#64748b">({_html.escape(ratio)} of expected)</span></p>'
        f'<p style="font-size:13px;color:#64748b">Measured {_html.escape(act_s)} · '
        f'Expected (matched) {_html.escape(exp_s)} · '
        f'{_html.escape(str(n if n is not None else "—"))} array(s)'
        f"{_html.escape(skip_bit)}</p>"
        f"{table}"
        f'<p style="font-size:12px;color:#64748b;margin-top:16px">'
        f"{_html.escape(_assumptions_blurb(snapshot))}</p>"
        f'<p style="font-size:11px;color:#94a3b8;margin-top:12px">'
        f"{_html.escape(footer)}</p></div>"
    )

    text_lines = [
        f"{company} — Performance Verification for {period}",
        "",
        f"Portfolio PI: {pi} ({ratio} of expected)",
        f"Measured: {act_s}  ·  Expected (matched): {exp_s}",
        f"Arrays verified: {n if n is not None else '—'}  ·  "
        f"Skipped: {skip if skip is not None else '—'}",
        "",
        "Per-array:",
        *(rows_text or ["  (none)"]),
        "",
        _assumptions_blurb(snapshot),
        "",
        footer,
    ]
    return html, "\n".join(text_lines)


def send_verification_report(tenant, snapshot: dict) -> dict:
    """Email the monthly verification pack (HTML body + PDF attachment).

    Skips demo tenants and tenants with verification_reports_enabled=False.
    Returns a status dict; never raises on send failure.
    """
    from .. import branding, notify
    from ..email_skin import render_email_skin, render_email_skin_text
    from ..stripe_helpers import ao_gets_vendor_emails

    tenant_id = getattr(tenant, "id", None)
    out: dict[str, Any] = {
        "ok": False,
        "tenant_id": tenant_id,
        "sent": False,
        "reason": None,
    }

    if getattr(tenant, "is_demo", False):
        out["reason"] = "demo_tenant"
        log.info("verification_report: skip demo tenant %s", tenant_id)
        return out

    if getattr(tenant, "verification_reports_enabled", True) is False:
        out["reason"] = "opted_out"
        log.info("verification_report: tenant %s opted out", tenant_id)
        return out

    if not ao_gets_vendor_emails(
        getattr(tenant, "product", None),
        getattr(tenant, "billing_plan", None),
    ):
        out["reason"] = "invoicing_only"
        log.info("verification_report: tenant %s invoicing-only — skip", tenant_id)
        return out

    to = (getattr(tenant, "contact_email", None) or "").strip()
    if not to:
        out["reason"] = "no_contact_email"
        log.info("verification_report: tenant %s has no contact_email", tenant_id)
        return out

    company = _fleet_name(tenant)
    period = _period_label(snapshot)
    body_html, body_text = render_verification_html(snapshot, company_name=company)

    try:
        pdf_bytes = render_verification_pdf(snapshot)
    except Exception as e:
        log.exception("verification_report: PDF failed for %s", tenant_id)
        out["reason"] = f"pdf_error: {e}"
        return out

    filename = f"performance-verification-{period}.pdf".replace(" ", "_")
    encoded = base64.b64encode(pdf_bytes).decode()
    attachments = [{"filename": filename, "content": encoded}]

    pi = None
    if snapshot.get("available"):
        pi = (snapshot.get("portfolio") or {}).get("performance_index")
    if pi is not None:
        subject = f"Performance Verification {period} · PI {_fmt_pi(pi)}"
    else:
        subject = f"Performance Verification {period} · no matched data"

    wrapped_html = render_email_skin(
        preheader=f"Monthly performance verification for {period}",
        headline="Performance Verification",
        intro_line=f"{company} · {period}",
        body_html=body_html,
        attachment_label=filename,
        attachment_size_bytes=len(pdf_bytes),
        product="array_operator",
    )
    wrapped_text = render_email_skin_text(
        headline="Performance Verification",
        intro_line=f"{company} · {period}",
        body_text=body_text,
        attachment_label=filename,
        product="array_operator",
    )

    try:
        sent = notify._send_via_resend(
            to=to,
            subject=subject,
            html=wrapped_html,
            text=wrapped_text,
            attachments=attachments,
            from_addr=branding.from_address("array_operator"),
            product="array_operator",
        )
    except Exception as e:
        log.exception("verification_report: send failed for %s", tenant_id)
        out["reason"] = f"send_error: {e}"
        return out

    out["ok"] = bool(sent)
    out["sent"] = bool(sent)
    out["reason"] = None if sent else "resend_failed"
    return out


def send_month_report_email(tenant, snap: dict, pdf_bytes: bytes | None = None) -> bool:
    """Backward-compatible wrapper used by early job drafts."""
    result = send_verification_report(tenant, snap)
    return bool(result.get("sent"))
