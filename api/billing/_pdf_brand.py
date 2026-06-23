"""
Shared PDF brand kit for Array Operator documents (invoice, performance summary,
quarterly report). One source of truth for the palette, the dark "energy" hero
band, and the juicy gradient energy bar chart, so every generated document looks
like the product.

Pulls its palette from the site (array-operator/public/styles.css :root).
Drawn entirely on the reportlab canvas — no extra dependencies.
"""
from __future__ import annotations

import math
from typing import Optional

# Brand palette (matches the Array Operator site — styles.css :root).
BG       = "#0a0e14"   # deep space background (hero band)
BG2      = "#0e131c"
INK      = "#eaf0f7"   # near-white text on dark
MUTED    = "#8b97a8"
GOOD     = "#3fd68a"   # signature energy green
GOOD2    = "#7ff0bb"   # bright green (glow / value text)
GREEN_DK = "#1f7d54"
GOLD     = "#f5b942"
SKY      = "#5ec2ff"
PAPER    = "#ffffff"
PAPER2   = "#f5f8fb"   # faint panel on white body
INKDK    = "#0f1722"   # near-black ink on white body
MUTEDDK  = "#5a6675"
LINE     = "#e5ebf1"

# Day (light) palette — matches the Array Operator DAY skin (theme-day.css):
# utility-blue accents on a light slate page, money figures in emerald.
PAPER_BG   = "#f6f8fb"   # light page / hero background
DAY_INK    = "#0f172a"   # slate-900 ink
DAY_MUTED  = "#64748b"   # slate-500
DAY_FAINT  = "#94a3b8"   # slate-400
DAY_BLUE   = "#2563eb"   # utility blue (day --good) — brand chrome
DAY_BLUE2  = "#0ea5e9"   # sky (day --good2)
DAY_GREEN  = "#047857"   # emerald-700 — the money / credit figure
DAY_LINE   = "#e2e8f0"   # slate-200 hairlines
DAY_PANEL  = "#eff6ff"   # blue-50 soft panel
DAY_GREENBG = "#ecfdf5"  # emerald-50 soft panel (amount-due)

HERO_H_DEFAULT = 1.55 * 72  # 1.55 inch in points


def _money(x: Optional[float]) -> str:
    return f"${(x or 0):,.2f}"


def _chart_label(lbl) -> str:
    """Short x-axis label for a bar. A 'YYYY-MM' (or 'YYYY-MM-DD') period label
    becomes a 3-letter month abbrev ('2026-06' → 'Jun'); anything else is
    truncated to 3 chars — the legacy behaviour for already-short month names
    like 'Jul'. (Without this, 'YYYY-MM' labels render as a clipped '202'.)"""
    import re
    import calendar
    s = str(lbl)
    m = re.match(r"^\d{4}-(\d{2})", s)
    if m:
        mi = int(m.group(1))
        if 1 <= mi <= 12:
            return calendar.month_abbr[mi]
    return s[:3]


def draw_energy_chart(c, x, y, w, h, points, accent=GOOD, empty_msg="No production data yet.",
                      light=False):
    """Draw a juicy gradient energy bar chart on the reportlab canvas.

    `points` is a list of (label, value) tuples — e.g. ("Jul", 3150.0). Bars use
    a vertical deep→bright gradient with a glow cap + value annotation on the
    peak. light=True paints utility-blue bars for the DAY skin. Never fabricates —
    caller passes only real values; an empty list renders the honest `empty_msg`.
    """
    from reportlab.lib import colors

    if light:
        good2_c = colors.HexColor(DAY_BLUE2)   # bar top (bright)
        green_dk_c = colors.HexColor(DAY_BLUE)  # bar bottom (deep)
        grid_c = colors.HexColor(DAY_LINE)
        muted_c = colors.HexColor(DAY_MUTED)
    else:
        good2_c = colors.HexColor(GOOD2)
        grid_c = colors.HexColor(LINE)
        muted_c = colors.HexColor(MUTEDDK)
        green_dk_c = colors.HexColor(GREEN_DK)

    pad_left, pad_bottom, pad_top = 6, 16, 12
    plot_x = x + pad_left
    plot_y = y + pad_bottom
    plot_w = w - pad_left - 6
    plot_h = h - pad_bottom - pad_top

    pts = [(_chart_label(lbl), float(v)) for lbl, v in points if v is not None]
    if not pts:
        c.setFillColor(muted_c)
        c.setFont("Helvetica", 8)
        c.drawString(x + 4, y + h / 2, empty_msg)
        return

    vmax = max((v for _, v in pts), default=0) or 1.0

    # Subtle horizontal gridlines.
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
        steps = 24
        for s in range(steps):
            t = s / steps
            seg_h = bh / steps
            sy = plot_y + bh * t
            col = colors.linearlyInterpolatedColor(green_dk_c, good2_c, 0, 1, t)
            c.setFillColor(col)
            c.rect(bx, sy, bar_w, seg_h + 0.6, fill=1, stroke=0)
        if i == peak_idx and bh > 0:
            c.setFillColor(good2_c)
            c.circle(cx, plot_y + bh + 3, 2.2, fill=1, stroke=0)
        c.setFillColor(muted_c)
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx, y + 5, label)

    _, pval = pts[peak_idx]
    c.setFillColor(green_dk_c)
    c.setFont("Helvetica-Bold", 7.5)
    pcx = plot_x + slot * (peak_idx + 0.5)
    c.drawCentredString(pcx, plot_y + (pval / vmax) * plot_h + 8, f"{pval:,.0f}")


def make_chart_flowable(points, width, height, accent=GOOD,
                        empty_msg="No production data yet.", light=False):
    """Build a reportlab Flowable that paints the energy bar chart."""
    from reportlab.platypus import Flowable

    class _Chart(Flowable):
        def __init__(self):
            Flowable.__init__(self)
            self.width = width
            self.height = height

        def wrap(self, aW, aH):
            return (width, height)

        def draw(self):
            draw_energy_chart(self.canv, 0, 0, width, height, points,
                              accent, empty_msg, light=light)

    return _Chart()


def make_hero_decorator(title, subtitle, right_label, right_value,
                        footer_left="Generated by Array Operator  ·  arrayoperator.com",
                        footer_right="", hero_h=HERO_H_DEFAULT, light=False,
                        show_brand=True):
    """Return an onPage(canvas, doc) callback that paints the energy hero band
    (brand mark + title + subtitle + a right-aligned figure) and a footer.
    Shared by every Array Operator document so the header is identical.

    light=True paints the DAY skin: a light slate band, dark ink, utility-blue
    brand chrome, and the headline figure in emerald (money) — matching the
    day-mode product. Default (False) keeps the dark 'energy' band.

    show_brand=False suppresses the Array Operator sun-glyph + wordmark so the
    document reads as the operator's OWN (white-label) — used for the offtaker
    invoice, which the operator sends to their customer under their own name.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch

    PAGE_W, PAGE_H = letter

    if light:
        band_c, ink_c, muted_c = colors.HexColor(PAPER_BG), colors.HexColor(DAY_INK), colors.HexColor(DAY_MUTED)
        brand_c, title_c, value_c, rule_c = (colors.HexColor(DAY_BLUE), colors.HexColor(DAY_INK),
                                             colors.HexColor(DAY_GREEN), colors.HexColor(DAY_BLUE))
        glow_rgb, foot_c = (0.149, 0.388, 0.921), colors.HexColor(DAY_MUTED)   # blue glow
    else:
        band_c, ink_c, muted_c = colors.HexColor(BG), colors.HexColor(INK), colors.HexColor(MUTED)
        brand_c, title_c, value_c, rule_c = (colors.HexColor(GOOD2), colors.HexColor(GOOD2),
                                             colors.HexColor(GOOD2), colors.HexColor(GOOD))
        glow_rgb, foot_c = (0.247, 0.839, 0.541), colors.HexColor(MUTEDDK)     # green glow

    def _decorate(c, doc):
        c.saveState()
        band_y = PAGE_H - hero_h
        c.setFillColor(band_c)
        c.rect(0, band_y, PAGE_W, hero_h, fill=1, stroke=0)
        # Radial-ish glow (stacked translucent ellipses, upper-right).
        for r, a in [(150, 0.05), (110, 0.06), (70, 0.08), (40, 0.10)]:
            c.setFillColor(colors.Color(*glow_rgb, alpha=a))
            cx = PAGE_W - 2.6 * inch
            cy = band_y + hero_h - 0.2 * inch
            c.ellipse(cx - r, cy - r, cx + r, cy + r, fill=1, stroke=0)
        # Accent rule under the band.
        c.setFillColor(rule_c)
        c.rect(0, band_y - 3, PAGE_W, 3, fill=1, stroke=0)
        # Brand mark (sun glyph) + wordmark — suppressed when white-labeled.
        if show_brand:
            gx, gy = 0.85 * inch, band_y + hero_h - 0.62 * inch
            c.setFillColor(brand_c)
            c.circle(gx, gy, 9, fill=1, stroke=0)
            c.setStrokeColor(brand_c)
            c.setLineWidth(1.4)
            for k in range(8):
                ang = k * math.pi / 4
                c.line(gx + 13 * math.cos(ang), gy + 13 * math.sin(ang),
                       gx + 17 * math.cos(ang), gy + 17 * math.sin(ang))
            c.setFillColor(ink_c)
            c.setFont("Helvetica-Bold", 13)
            c.drawString(gx + 26, gy - 5, "Array Operator")
        # Title + subtitle. Without the wordmark the band would be top-heavy with
        # whitespace, so center the title/subtitle a little higher.
        title_y = band_y + (0.46 if show_brand else 0.62) * inch
        sub_y = band_y + (0.27 if show_brand else 0.42) * inch
        c.setFillColor(title_c)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(0.85 * inch, title_y, title)
        if subtitle:
            c.setFillColor(muted_c)
            c.setFont("Helvetica", 9.5)
            c.drawString(0.85 * inch, sub_y, subtitle)
        # Right-aligned figure (amount due / lifetime / etc.).
        if right_value:
            c.setFont("Helvetica", 8)
            c.setFillColor(muted_c)
            c.drawRightString(PAGE_W - 0.85 * inch, band_y + 0.62 * inch, right_label)
            c.setFont("Helvetica-Bold", 26)
            c.setFillColor(value_c)
            c.drawRightString(PAGE_W - 0.85 * inch, band_y + 0.32 * inch, right_value)
        # Footer.
        c.setFillColor(foot_c)
        c.setFont("Helvetica", 7.5)
        c.drawString(0.85 * inch, 0.45 * inch, footer_left)
        if footer_right:
            c.drawRightString(PAGE_W - 0.85 * inch, 0.45 * inch, footer_right)
        c.restoreState()

    return _decorate
