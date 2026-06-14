"""
Performance summary — the second half of the "both" report Ford asked for.

Where the invoice says "here's what you owe", the summary says "here's how your
array performed": production this period vs. the same period a year ago, the
trailing-twelve-month total, and lifetime generation + savings. All derived from
the workbook's own ledger (no telemetry needed); when the customer's array is
telemetry-connected the caller can pass `peer` health to enrich it.

  build_summary(match)        → plain dict of the computed metrics
  render_summary_xlsx(match)  → one-sheet .xlsx
  render_summary_pdf(match)   → one-page PDF (reportlab)
"""
from __future__ import annotations

import pathlib
from typing import Optional

from .matcher import BillingMatch, Period


def _same_month_prior_year(periods: list[Period], latest: Period) -> Optional[Period]:
    if not latest.end:
        return None
    target_year = latest.end.year - 1
    best = None
    for p in periods:
        if p.end and p.end.year == target_year and p.end.month == latest.end.month:
            best = p
    return best


def build_summary(match: BillingMatch, peer: Optional[dict] = None) -> dict:
    periods = [p for p in match.periods if (p.customer_kwh or p.array_kwh)]
    latest = match.latest_period
    out: dict = {
        "customer_name": match.customer.get("name"),
        "lifetime_kwh": match.project_totals.get("total_customer_kwh"),
        "lifetime_savings": match.project_totals.get("total_savings"),
        "period_count": match.project_totals.get("period_count"),
        "this_period_kwh": (latest.customer_kwh if latest else None),
        "this_period_month": (latest.month if latest else None),
        "this_period_end": (latest.end.isoformat() if latest and latest.end else None),
    }
    # Year-over-year for the latest period.
    if latest:
        prior = _same_month_prior_year(periods, latest)
        if prior and prior.customer_kwh and latest.customer_kwh:
            delta = latest.customer_kwh - prior.customer_kwh
            out["yoy_prior_kwh"] = prior.customer_kwh
            out["yoy_delta_kwh"] = round(delta, 1)
            out["yoy_delta_pct"] = round(100 * delta / prior.customer_kwh, 1)
    # Trailing twelve months (last 12 dated periods).
    dated = sorted([p for p in periods if p.end], key=lambda p: p.end)  # type: ignore[arg-type]
    ttm = dated[-12:]
    out["ttm_kwh"] = round(sum(p.customer_kwh or 0 for p in ttm), 1)
    out["ttm_savings"] = round(sum(p.savings or 0 for p in ttm), 2)
    out["ttm_points"] = [
        {"month": p.month, "end": p.end.isoformat() if p.end else None,
         "kwh": p.customer_kwh}
        for p in ttm
    ]
    if peer:
        out["peer"] = peer
    return out


def render_summary_xlsx(match: BillingMatch, out_path: pathlib.Path,
                        peer: Optional[dict] = None) -> pathlib.Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    s = build_summary(match, peer)
    wb = Workbook()
    ws = wb.active
    ws.title = "Performance"
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 22
    bold = Font(bold=True, size=13)
    ws["A1"] = f"Performance summary — {s.get('customer_name') or 'Array'}"
    ws["A1"].font = bold

    rows = [
        ("This period (kWh)", s.get("this_period_kwh")),
        ("This period", s.get("this_period_month")),
        ("Year-over-year change (kWh)", s.get("yoy_delta_kwh")),
        ("Year-over-year change (%)", s.get("yoy_delta_pct")),
        ("Trailing 12 months (kWh)", s.get("ttm_kwh")),
        ("Trailing 12 months savings", s.get("ttm_savings")),
        ("Lifetime generation (kWh)", s.get("lifetime_kwh")),
        ("Lifetime savings ($)", s.get("lifetime_savings")),
        ("Billing periods on record", s.get("period_count")),
    ]
    r = 3
    for label, val in rows:
        ws.cell(row=r, column=1, value=label)
        ws.cell(row=r, column=2, value=val)
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Trailing 12 months").font = Font(bold=True)
    r += 1
    ws.cell(row=r, column=1, value="Month")
    ws.cell(row=r, column=2, value="kWh")
    r += 1
    for pt in s.get("ttm_points", []):
        ws.cell(row=r, column=1, value=pt.get("month") or pt.get("end"))
        ws.cell(row=r, column=2, value=pt.get("kwh"))
        r += 1

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def render_summary_pdf(match: BillingMatch, out_path: pathlib.Path,
                       peer: Optional[dict] = None) -> pathlib.Path:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("reportlab is required for PDF summaries") from e

    s = build_summary(match, peer)
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    GREEN = colors.HexColor("#047857")
    h = ParagraphStyle("h", parent=styles["Title"], textColor=GREEN, fontSize=18)
    lbl = ParagraphStyle("lbl", parent=styles["Normal"], fontSize=10)

    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    story = [Paragraph(f"Performance summary — {s.get('customer_name') or 'Array'}", h),
             Spacer(1, 14)]

    def fmt(v, money=False, pct=False):
        if v is None:
            return "—"
        if money:
            return f"${v:,.2f}"
        if pct:
            return f"{v:+.1f}%"
        return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)

    rows = [
        ["This period", f"{fmt(s.get('this_period_kwh'))} kWh  ({s.get('this_period_month') or ''})"],
        ["Year over year", f"{fmt(s.get('yoy_delta_kwh'))} kWh  ({fmt(s.get('yoy_delta_pct'), pct=True)})"
            if s.get("yoy_delta_kwh") is not None else "—"],
        ["Trailing 12 months", f"{fmt(s.get('ttm_kwh'))} kWh"],
        ["Trailing 12-mo savings", fmt(s.get("ttm_savings"), money=True)],
        ["Lifetime generation", f"{fmt(s.get('lifetime_kwh'))} kWh"],
        ["Lifetime savings", fmt(s.get("lifetime_savings"), money=True)],
        ["Periods on record", fmt(s.get("period_count"))],
    ]
    if s.get("peer"):
        p = s["peer"]
        rows.append(["Peer-measured health", str(p.get("status") or p)])
    t = Table(rows, colWidths=[2.6 * inch, 3.6 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Generation and savings are computed from your billing ledger. "
        "Year-over-year compares the same billing month one year earlier.", lbl))
    doc.build(story)
    return out_path
