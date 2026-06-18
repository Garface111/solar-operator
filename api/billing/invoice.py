"""
Invoice generators — rebuild the HCT "Template" invoice from a BillingMatch.

Two output formats, the operator picks per subscription:
  render_invoice_xlsx — an .xlsx that mirrors the workbook's own Template sheet.
  render_invoice_pdf  — a clean PDF the customer can pay against (reportlab).

Both consume a `BillingMatch` (api/billing/matcher.py) and a chosen `Period`
(defaults to the latest). `invoice_for_period` does the dollar math via the
shared `compute_invoice` so XLSX and PDF never drift.
"""
from __future__ import annotations

import pathlib
from datetime import date, timedelta
from typing import Optional

from .matcher import BillingMatch, Period, compute_invoice

DUE_DAYS = 28


def invoice_for_period(match: BillingMatch, period: Period,
                       invoice_date: date) -> dict:
    """Assemble the full invoice payload for one period (dates + dollar math)."""
    inv = compute_invoice(
        period.customer_kwh, period.tariff, period.adder,
        match.billing_rate, match.billing_model,
        match.template.get("fixed_amount"),
    )
    due = invoice_date + timedelta(days=DUE_DAYS)
    inv.update({
        "invoice_number": period.end.strftime("%Y-%m") if period.end else invoice_date.strftime("%Y-%m"),
        "invoice_date": invoice_date.isoformat(),
        "due_date": due.isoformat(),
        "period_start": period.start.isoformat() if period.start else None,
        "period_end": period.end.isoformat() if period.end else None,
        "month": period.month,
        "customer_name": match.customer.get("name"),
        "billing_model": match.billing_model,
        "allocation_pct": match.allocation_pct,
        "project_total_kwh": match.project_totals.get("total_customer_kwh"),
        "project_total_savings": match.project_totals.get("total_savings"),
    })
    return inv


def _money(x: Optional[float]) -> str:
    return f"${(x or 0):,.2f}"


def _pct(x: Optional[float]) -> str:
    return f"{round((x or 0) * 100):g}%"


# ─── XLSX ───────────────────────────────────────────────────────────────────

def render_invoice_xlsx(match: BillingMatch, out_path: pathlib.Path,
                        period: Optional[Period] = None,
                        invoice_date: Optional[date] = None) -> pathlib.Path:
    """Write an .xlsx invoice mirroring the HCT Template sheet layout."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    period = period or match.latest_period
    if period is None:
        raise ValueError("no billing period to invoice")
    invoice_date = invoice_date or date.today()
    inv = invoice_for_period(match, period, invoice_date)
    tpl = match.template

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 24
    bold = Font(bold=True)
    big = Font(bold=True, size=14)
    right = Alignment(horizontal="right")

    def put(cell, value, fnt=None, align=None):
        ws[cell] = value
        if fnt:
            ws[cell].font = fnt
        if align:
            ws[cell].alignment = align

    put("B2", tpl.get("title", "Invoice - Solar Power Generation"), big)
    put("B4", tpl.get("operator", "HCT Sun Enterprises, LLC"), bold)
    if tpl.get("phone"):
        put("C4", tpl["phone"])
    if tpl.get("attn"):
        put("C5", tpl["attn"])
    if match.customer.get("email") or tpl.get("email"):
        put("C6", "email: " + (match.customer.get("email") or tpl.get("email")))

    put("B8", inv["customer_name"] or "Customer", bold)
    put("B9", "Invoice Number:"); put("C9", inv["invoice_number"], align=right)
    put("B10", "Invoice Date:"); put("C10", inv["invoice_date"], align=right)
    put("B11", "Due Date (28 days):"); put("C11", inv["due_date"], align=right)
    put("B12", "Time Period Covered:")
    put("C12", f"{inv['period_start']} → {inv['period_end']}", align=right)

    put("B14", "Note — actual results for the billing period", bold)
    put("B15", "kWh:"); put("C15", round(inv["kwh"], 0), align=right)
    put("B16", f"Solar Credit Rate: ${inv['tariff']:.5f}/kWh")
    put("C16", _money(inv["net_value"]), align=right)
    put("B17", f"Incentive Rate: ${inv['adder']:.5f}/kWh")
    put("C17", _money(inv["incentive_value"]), align=right)
    put("B18", "Solar Value:"); put("C18", _money(inv["solar_value"]), align=right)
    put("B19", f"Billing Rate: {_pct(inv['billing_rate'])}")
    put("C19", _money(inv["billed_value"]), align=right)
    put("B20", "Solar Savings:"); put("C20", _money(inv["solar_savings"]), align=right)

    put("B22", "Amount Owed:", big)
    put("C22", _money(inv["amount_owed"]), big, right)
    put("B23", tpl.get("payable_to", "Please make check payable to HCT Sun Enterprises, LLC"))

    put("B26", "Project Total kWh generation:")
    put("C26", round(inv.get("project_total_kwh") or 0, 0), align=right)
    put("B27", "Project total financial savings:")
    put("C27", _money(inv.get("project_total_savings")), align=right)

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


# ─── PDF ────────────────────────────────────────────────────────────────────

def render_invoice_pdf(match: BillingMatch, out_path: pathlib.Path,
                       period: Optional[Period] = None,
                       invoice_date: Optional[date] = None) -> pathlib.Path:
    """Write a clean one-page PDF invoice via reportlab."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("reportlab is required for PDF invoices") from e

    period = period or match.latest_period
    if period is None:
        raise ValueError("no billing period to invoice")
    invoice_date = invoice_date or date.today()
    inv = invoice_for_period(match, period, invoice_date)
    tpl = match.template

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    GREEN = colors.HexColor("#047857")
    h = ParagraphStyle("h", parent=styles["Title"], textColor=GREEN, fontSize=18)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#555"))
    lbl = ParagraphStyle("lbl", parent=styles["Normal"], fontSize=10)

    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    story = []
    story.append(Paragraph(tpl.get("title", "Invoice — Solar Power Generation"), h))
    story.append(Spacer(1, 4))
    op_lines = [tpl.get("operator", "HCT Sun Enterprises, LLC")]
    if tpl.get("attn"):
        op_lines.append(tpl["attn"])
    if tpl.get("phone"):
        op_lines.append(tpl["phone"])
    if match.customer.get("email") or tpl.get("email"):
        op_lines.append("email: " + (match.customer.get("email") or tpl.get("email")))
    story.append(Paragraph(" &nbsp;·&nbsp; ".join(op_lines), sub))
    story.append(Spacer(1, 16))

    meta = [
        ["Bill to:", inv["customer_name"] or "Customer"],
        ["Invoice number:", inv["invoice_number"]],
        ["Invoice date:", inv["invoice_date"]],
        ["Due date (28 days):", inv["due_date"]],
        ["Period covered:", f"{inv['period_start']}  →  {inv['period_end']}"],
    ]
    mt = Table(meta, colWidths=[1.8 * inch, 4.4 * inch])
    mt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(mt)
    story.append(Spacer(1, 16))
    story.append(Paragraph("Actual results for the billing period", lbl))
    story.append(Spacer(1, 6))

    rows = [
        ["kWh", f"{inv['kwh']:,.0f}"],
        [f"Solar credit rate — ${inv['tariff']:.5f}/kWh", _money(inv["net_value"])],
        [f"Incentive rate — ${inv['adder']:.5f}/kWh", _money(inv["incentive_value"])],
        ["Solar value", _money(inv["solar_value"])],
        [f"Billing rate — {_pct(inv['billing_rate'])}", _money(inv["billed_value"])],
        ["Solar savings", _money(inv["solar_savings"])],
    ]
    rt = Table(rows, colWidths=[4.2 * inch, 2.0 * inch])
    rt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(rt)
    story.append(Spacer(1, 14))

    owed = Table([["Amount due", _money(inv["amount_owed"])]],
                 colWidths=[4.2 * inch, 2.0 * inch])
    owed.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfdf5")),
        ("TEXTCOLOR", (0, 0), (-1, -1), GREEN),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(owed)
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        tpl.get("payable_to", "Please make check payable to HCT Sun Enterprises, LLC"), sub))
    story.append(Spacer(1, 18))
    story.append(Paragraph(
        f"Project total generation: {(inv.get('project_total_kwh') or 0):,.0f} kWh"
        f" &nbsp;·&nbsp; Project total savings: {_money(inv.get('project_total_savings'))}", sub))

    doc.build(story)
    return out_path
