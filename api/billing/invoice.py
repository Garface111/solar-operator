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
        # Per-array breakdown for multi-array offtakers (one line per array). Lives
        # on project_totals/computed_invoice; surfaced here so the PDF can render it.
        "array_breakdown": match.project_totals.get("array_breakdown")
            or (match.computed_invoice or {}).get("array_breakdown"),
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
# Shared brand kit (palette, dark energy hero band, energy bar chart) — one
# source of truth across invoice / performance summary / quarterly report.
from . import _pdf_brand as brand


def render_invoice_pdf(match: BillingMatch, out_path: pathlib.Path,
                       period: Optional[Period] = None,
                       invoice_date: Optional[date] = None) -> pathlib.Path:
    """Write a slick, on-brand one-page PDF invoice (reportlab).

    Design: a dark 'energy' hero band (Array Operator green glow) up top, a clean
    payable invoice body in the middle, and a juicy monthly-energy bar chart at
    the bottom. Matches the site palette so the document feels like the product.
    """
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

    HERO_H = 1.55 * inch
    GREEN_DK = colors.HexColor(brand.GREEN_DK)
    INKDK = colors.HexColor(brand.INKDK)
    MUTEDDK = colors.HexColor(brand.MUTEDDK)
    operator_name = tpl.get("operator", "HCT Sun Enterprises, LLC")
    title = tpl.get("title", "Solar Power Invoice")

    decorate = brand.make_hero_decorator(
        title=title, subtitle=operator_name,
        right_label="AMOUNT DUE", right_value=brand._money(inv["amount_owed"]),
        footer_right=f"Invoice {inv['invoice_number']}", hero_h=HERO_H)

    styles = getSampleStyleSheet()
    lbl = ParagraphStyle("lbl", parent=styles["Normal"], fontSize=11,
                         textColor=INKDK, fontName="Helvetica-Bold")
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5,
                           textColor=MUTEDDK)
    # Wrapping style for the bill-to / period values: a long customer name must
    # WRAP within its column, not overflow into the period cell (plain table
    # strings don't wrap → they bleed across the column boundary).
    billval = ParagraphStyle("billval", parent=styles["Normal"], fontSize=13,
                             leading=15, fontName="Helvetica-Bold",
                             textColor=INKDK)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter,
        topMargin=HERO_H + 0.35 * inch, bottomMargin=0.8 * inch,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch)
    story = []

    # ---- meta block (bill-to + dates) ----------------------------------------
    from xml.sax.saxutils import escape as _xesc
    meta = [
        ["BILL TO", "PERIOD"],
        [Paragraph(_xesc(inv["customer_name"] or "Customer"), billval),
         Paragraph(f"{_xesc(inv['period_start'] or '')}  →  "
                   f"{_xesc(inv['period_end'] or '')}", billval)],
        ["", ""],
        ["Invoice date", inv["invoice_date"]],
        ["Due date (28 days)", inv["due_date"]],
    ]
    mt = Table(meta, colWidths=[3.3 * inch, 3.3 * inch])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTEDDK),
        ("VALIGN", (0, 1), (-1, 1), "TOP"),
        ("FONTSIZE", (0, 3), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 3), (0, -1), MUTEDDK),
        ("TEXTCOLOR", (1, 3), (1, -1), INKDK),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(mt)
    story.append(Spacer(1, 18))

    # ---- per-array breakdown (multi-array offtakers) -------------------------
    # When the offtaker owns a share of several arrays, show one line per array
    # (array · its production · this offtaker's % · their kWh) so the summed
    # "Energy produced" total below is auditable. Single-array invoices skip this.
    breakdown = inv.get("array_breakdown") or []
    if len(breakdown) > 1:
        story.append(Paragraph("Your share by array", lbl))
        story.append(Spacer(1, 8))
        brows = [["Array", "Array produced", "Your %", "Your kWh"]]
        for b in breakdown:
            brows.append([
                str(b.get("array_name") or "Array"),
                f"{float(b.get('array_kwh') or 0):,.0f} kWh",
                _pct(b.get("allocation_pct") or 0),
                f"{float(b.get('customer_kwh') or 0):,.0f} kWh",
            ])
        brows.append(["Total", "", "", f"{inv['kwh']:,.0f} kWh"])
        bt = Table(brows, colWidths=[2.7 * inch, 1.5 * inch, 0.9 * inch, 1.5 * inch])
        bt.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (-1, -1), INKDK),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor(brand.LINE)),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor(brand.LINE)),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(bt)
        story.append(Spacer(1, 16))

    # ---- line items ----------------------------------------------------------
    story.append(Paragraph("Actual results for the billing period", lbl))
    story.append(Spacer(1, 8))
    rows = [
        ["Energy produced", f"{inv['kwh']:,.0f} kWh"],
        [f"Solar credit rate  ·  ${inv['tariff']:.5f}/kWh", _money(inv["net_value"])],
        [f"Incentive rate  ·  ${inv['adder']:.5f}/kWh", _money(inv["incentive_value"])],
        ["Solar value", _money(inv["solar_value"])],
        [f"Billing rate  ·  {_pct(inv['billing_rate'])}", _money(inv["billed_value"])],
        ["Your solar savings", _money(inv["solar_savings"])],
    ]
    rt = Table(rows, colWidths=[4.4 * inch, 2.2 * inch])
    rt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, -1), INKDK),
        ("TEXTCOLOR", (1, -1), (1, -1), GREEN_DK),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor(brand.LINE)),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(rt)
    story.append(Spacer(1, 16))

    # ---- amount due banner (juicy green) -------------------------------------
    owed = Table([["AMOUNT DUE", _money(inv["amount_owed"])]],
                 colWidths=[4.4 * inch, 2.2 * inch])
    owed.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (0, 0), 11),
        ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (1, 0), (1, 0), 18),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#06140d")),
        ("TEXTCOLOR", (0, 0), (0, 0), colors.HexColor(brand.GOOD2)),
        ("TEXTCOLOR", (1, 0), (1, 0), colors.HexColor(brand.GOOD2)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 13),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("ROUNDEDCORNERS", [7, 7, 7, 7]),
    ]))
    story.append(owed)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        tpl.get("payable_to", f"Please make check payable to {operator_name}."),
        small))
    story.append(Spacer(1, 22))

    # ---- monthly energy chart (the juicy finish) -----------------------------
    story.append(Paragraph("Monthly energy produced", lbl))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Your share of the array's generation, by month  ·  "
        f"project total {(inv.get('project_total_kwh') or 0):,.0f} kWh", small))
    story.append(Spacer(1, 8))
    # Most recent ~12 months that carry a kWh value (never fabricated).
    pts = [((p.month or (p.end.strftime("%b") if p.end else "")), p.customer_kwh)
           for p in match.periods if p.customer_kwh is not None][-12:]
    story.append(brand.make_chart_flowable(
        pts, 6.6 * inch, 1.7 * inch,
        empty_msg="No monthly production data yet."))

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return out_path
