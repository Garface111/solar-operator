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
# Brand palette (matches the Array Operator site — styles.css :root).
_BG       = "#0a0e14"   # deep space background (hero band)
_BG2      = "#0e131c"
_INK      = "#eaf0f7"   # near-white text on dark
_MUTED    = "#8b97a8"
_GOOD     = "#3fd68a"   # signature energy green
_GOOD2    = "#7ff0bb"   # bright green (glow / value text)
_GREEN_DK = "#1f7d54"
_GOLD     = "#f5b942"
_SKY      = "#5ec2ff"
_PAPER    = "#ffffff"
_PAPER2   = "#f5f8fb"   # faint panel on white body
_INKDK    = "#0f1722"   # near-black ink on white body
_MUTEDDK  = "#5a6675"
_LINE     = "#e5ebf1"


def _draw_monthly_energy_chart(c, x, y, w, h, periods, accent="#3fd68a"):
    """Draw a juicy gradient monthly-energy bar chart on the reportlab canvas.

    `periods` is a list of Period objects; we plot customer_kwh per month. Bars
    use a vertical green gradient with a soft glow cap. Never fabricates — months
    with no kWh render as a faint zero baseline tick, not invented values.
    """
    from reportlab.lib import colors

    # Take the most recent ~12 months that carry a kWh value.
    pts = []
    for p in periods:
        kwh = getattr(p, "customer_kwh", None)
        label = (getattr(p, "month", None) or
                 (p.end.strftime("%b") if getattr(p, "end", None) else "") or "")
        if kwh is not None:
            pts.append((str(label)[:3], float(kwh)))
    pts = pts[-12:]

    accent_c = colors.HexColor(accent)
    good2_c = colors.HexColor(_GOOD2)
    grid_c = colors.HexColor(_LINE)
    muted_c = colors.HexColor(_MUTEDDK)

    # Plot frame
    pad_left, pad_bottom, pad_top = 6, 16, 10
    plot_x = x + pad_left
    plot_y = y + pad_bottom
    plot_w = w - pad_left - 6
    plot_h = h - pad_bottom - pad_top

    if not pts:
        c.setFillColor(muted_c)
        c.setFont("Helvetica", 8)
        c.drawString(x + 4, y + h / 2, "No monthly production data yet.")
        return

    vmax = max((v for _, v in pts), default=0) or 1.0

    # Horizontal gridlines (3) — subtle.
    c.setStrokeColor(grid_c)
    c.setLineWidth(0.5)
    for i in range(4):
        gy = plot_y + plot_h * i / 3
        c.line(plot_x, gy, plot_x + plot_w, gy)

    n = len(pts)
    slot = plot_w / n
    bar_w = min(slot * 0.6, 26)
    peak_idx = max(range(n), key=lambda i: pts[i][1])

    for i, (label, v) in enumerate(pts):
        cx = plot_x + slot * (i + 0.5)
        bx = cx - bar_w / 2
        bh = (v / vmax) * plot_h if vmax else 0
        # Vertical gradient: deep green base → bright green top (juicy).
        steps = 24
        for s in range(steps):
            t = s / steps
            seg_h = bh / steps
            sy = plot_y + bh * t
            # interpolate green-deep → good → good2
            col = colors.linearlyInterpolatedColor(
                colors.HexColor(_GREEN_DK), good2_c, 0, 1, t)
            c.setFillColor(col)
            c.rect(bx, sy, bar_w, seg_h + 0.6, fill=1, stroke=0)
        # Glow cap on the peak month.
        if i == peak_idx and bh > 0:
            c.setFillColor(good2_c)
            c.circle(cx, plot_y + bh + 3, 2.2, fill=1, stroke=0)
        # Month label.
        c.setFillColor(muted_c)
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx, y + 5, label)

    # Peak value annotation.
    plabel, pval = pts[peak_idx]
    c.setFillColor(colors.HexColor(_GREEN_DK))
    c.setFont("Helvetica-Bold", 7.5)
    pcx = plot_x + slot * (peak_idx + 0.5)
    c.drawCentredString(pcx, plot_y + (pval / vmax) * plot_h + 8,
                        f"{pval:,.0f}")


def _make_chart_flowable(periods, width, height, accent="#3fd68a"):
    """Build a reportlab Flowable that paints the monthly-energy bar chart."""
    from reportlab.platypus import Flowable

    class _Chart(Flowable):
        def __init__(self):
            Flowable.__init__(self)
            self.width = width
            self.height = height

        def wrap(self, aW, aH):
            return (width, height)

        def draw(self):
            # self.canv origin is the flowable's lower-left.
            _draw_monthly_energy_chart(self.canv, 0, 0, width, height,
                                       periods, accent)

    return _Chart()


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
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable)
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

    PAGE_W, PAGE_H = letter
    HERO_H = 1.55 * inch
    GREEN = colors.HexColor(_GOOD)
    GREEN_DK = colors.HexColor(_GREEN_DK)
    INKDK = colors.HexColor(_INKDK)
    MUTEDDK = colors.HexColor(_MUTEDDK)
    operator_name = tpl.get("operator", "HCT Sun Enterprises, LLC")
    title = tpl.get("title", "Solar Power Invoice")

    # ---- page furniture: dark hero band + footer, painted on the canvas ------
    def _decorate(c, doc):
        c.saveState()
        # Hero band background (deep space + subtle vertical gradient).
        band_y = PAGE_H - HERO_H
        c.setFillColor(colors.HexColor(_BG))
        c.rect(0, band_y, PAGE_W, HERO_H, fill=1, stroke=0)
        # Radial-ish green glow (stacked translucent ellipses, top-left).
        for r, a in [(150, 0.05), (110, 0.06), (70, 0.08), (40, 0.10)]:
            c.setFillColor(colors.Color(0.247, 0.839, 0.541, alpha=a))
            c.ellipse(PAGE_W - 2.6 * inch - r, band_y + HERO_H - 0.2 * inch - r,
                      PAGE_W - 2.6 * inch + r, band_y + HERO_H - 0.2 * inch + r,
                      fill=1, stroke=0)
        # Bright accent rule under the band.
        c.setFillColor(GREEN)
        c.rect(0, band_y - 3, PAGE_W, 3, fill=1, stroke=0)
        # Brand mark (sun glyph) + wordmark.
        gx, gy = 0.85 * inch, band_y + HERO_H - 0.62 * inch
        c.setFillColor(colors.HexColor(_GOOD2))
        c.circle(gx, gy, 9, fill=1, stroke=0)
        c.setStrokeColor(colors.HexColor(_GOOD2))
        c.setLineWidth(1.4)
        import math as _m
        for k in range(8):
            ang = k * _m.pi / 4
            c.line(gx + 13 * _m.cos(ang), gy + 13 * _m.sin(ang),
                   gx + 17 * _m.cos(ang), gy + 17 * _m.sin(ang))
        c.setFillColor(colors.HexColor(_INK))
        c.setFont("Helvetica-Bold", 13)
        c.drawString(gx + 26, gy - 5, "Array Operator")
        # Title + operator.
        c.setFillColor(colors.HexColor(_GOOD2))
        c.setFont("Helvetica-Bold", 20)
        c.drawString(0.85 * inch, band_y + 0.46 * inch, title)
        c.setFillColor(colors.HexColor(_MUTED))
        c.setFont("Helvetica", 9.5)
        c.drawString(0.85 * inch, band_y + 0.27 * inch, operator_name)
        # Amount-due chip, right-aligned in the hero.
        chip_txt = _money(inv["amount_owed"])
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor(_MUTED))
        c.drawRightString(PAGE_W - 0.85 * inch, band_y + 0.62 * inch, "AMOUNT DUE")
        c.setFont("Helvetica-Bold", 26)
        c.setFillColor(colors.HexColor(_GOOD2))
        c.drawRightString(PAGE_W - 0.85 * inch, band_y + 0.32 * inch, chip_txt)
        # Footer.
        c.setFillColor(MUTEDDK)
        c.setFont("Helvetica", 7.5)
        c.drawString(0.85 * inch, 0.45 * inch,
                     "Generated by Array Operator  ·  arrayoperator.com")
        c.drawRightString(PAGE_W - 0.85 * inch, 0.45 * inch,
                          f"Invoice {inv['invoice_number']}")
        c.restoreState()

    styles = getSampleStyleSheet()
    lbl = ParagraphStyle("lbl", parent=styles["Normal"], fontSize=11,
                         textColor=INKDK, fontName="Helvetica-Bold")
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5,
                           textColor=MUTEDDK)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=letter,
        topMargin=HERO_H + 0.35 * inch, bottomMargin=0.8 * inch,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch)
    story = []

    # ---- meta block (bill-to + dates) ----------------------------------------
    meta = [
        ["BILL TO", "PERIOD"],
        [inv["customer_name"] or "Customer",
         f"{inv['period_start']}  →  {inv['period_end']}"],
        ["", ""],
        ["Invoice date", inv["invoice_date"]],
        ["Due date (28 days)", inv["due_date"]],
    ]
    mt = Table(meta, colWidths=[3.3 * inch, 3.3 * inch])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTEDDK),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 13),
        ("TEXTCOLOR", (0, 1), (-1, 1), INKDK),
        ("FONTSIZE", (0, 3), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 3), (0, -1), MUTEDDK),
        ("TEXTCOLOR", (1, 3), (1, -1), INKDK),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(mt)
    story.append(Spacer(1, 18))

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
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor(_LINE)),
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
        ("TEXTCOLOR", (0, 0), (0, 0), colors.HexColor(_GOOD2)),
        ("TEXTCOLOR", (1, 0), (1, 0), colors.HexColor(_GOOD2)),
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
    chart_w = 6.6 * inch
    story.append(_make_chart_flowable(match.periods, chart_w, 1.7 * inch, accent=_GOOD))

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return out_path
