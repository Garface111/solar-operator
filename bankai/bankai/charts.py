"""Email-safe data visualizations — charts built from tables and inline styles.

Email clients are a hostile rendering target: Gmail strips <svg>, <canvas>, and
<style> blocks, and ignores most modern CSS. What survives everywhere is tables,
background-color, and width percentages — so every chart here is drawn with
those primitives and nothing else. A horizontal bar is a table row with a
colored cell sized by percentage; a stat tile is a padded table cell. No image
generation, no external requests, no fragile CSS.

These are the pieces the copilot composes with (via ```barchart / ```stats /
```progress fenced blocks in its markdown) so a spending breakdown arrives as
bars, a net-worth figure as a stat tile, and a debt goal as a progress meter —
graphics carrying the numbers, not just prose about them.
"""
from __future__ import annotations

import html
import re

INK = "#1a2b3c"
MUTED = "#6b7a8b"
TRACK = "#eef2f6"
ACCENT = "#0f6b57"

# A calm categorical palette, reused in order.
SERIES = ["#0f6b57", "#3b82f6", "#7c3aed", "#d97706", "#0891b2",
          "#be185d", "#65a30d", "#dc2626", "#0d9488", "#7c3aed"]


def _num(raw: str) -> float:
    """Parse a value like '$1,234.56', '1.2M', '45%' into a float for sizing."""
    s = (raw or "").strip().lower().replace(",", "").replace("$", "").replace("%", "")
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    elif s.endswith("b"):
        mult, s = 1_000_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def _rows(block: str) -> list[list[str]]:
    """Parse 'a | b | c' lines into cell lists, skipping blanks."""
    out = []
    for line in (block or "").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append([c.strip() for c in line.split("|")])
    return out


def bar_chart(block: str) -> str:
    """Horizontal bar chart. Each line: ``Label | value`` (optional ``| #color``
    or ``| display``). The numeric magnitude sizes the bar; the original text is
    shown as the value so '$1,234' or '45%' displays as written."""
    rows = _rows(block)
    parsed = []
    for i, cells in enumerate(rows):
        label = cells[0] if cells else ""
        value_txt = cells[1] if len(cells) > 1 else "0"
        color = SERIES[i % len(SERIES)]
        display = value_txt
        for extra in cells[2:]:
            if extra.startswith("#"):
                color = extra
            else:
                display = extra
        parsed.append((label, _num(value_txt), display, color))
    if not parsed:
        return ""
    top = max((v for _, v, _, _ in parsed), default=0.0) or 1.0
    lines = []
    for label, value, display, color in parsed:
        pct = max(2, round(100 * value / top))
        bar = (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;table-layout:fixed;"><tr>'
            f'<td width="{pct}%" style="background:{color};height:16px;line-height:16px;'
            'font-size:0;border-radius:4px;">&nbsp;</td>'
            f'<td style="font-size:0;line-height:16px;">&nbsp;</td></tr></table>'
        )
        lines.append(
            '<tr>'
            f'<td style="padding:5px 10px 5px 0;font-size:13px;color:{INK};'
            f'white-space:nowrap;vertical-align:middle;width:34%;">{html.escape(label)}</td>'
            f'<td style="padding:5px 0;vertical-align:middle;">{bar}</td>'
            f'<td style="padding:5px 0 5px 10px;font-size:13px;font-weight:700;color:{INK};'
            f'text-align:right;white-space:nowrap;vertical-align:middle;width:20%;">'
            f'{html.escape(display)}</td>'
            '</tr>'
        )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;margin:16px 0;">{"".join(lines)}</table>'
    )


def stat_cards(block: str) -> str:
    """A row of big-number tiles. Each line: ``Value | Label`` (optional
    ``| #accent``). Wraps to a second row past four tiles."""
    rows = _rows(block)
    tiles = []
    for i, cells in enumerate(rows):
        value = cells[0] if cells else ""
        label = cells[1] if len(cells) > 1 else ""
        accent = cells[2] if len(cells) > 2 and cells[2].startswith("#") else ACCENT
        tiles.append((value, label, accent))
    if not tiles:
        return ""
    cells_html = []
    for value, label, accent in tiles:
        cells_html.append(
            '<td style="padding:6px;vertical-align:top;" width="25%">'
            f'<div style="background:#f7f9fb;border:1px solid #e4e9ef;border-top:3px solid {accent};'
            'border-radius:8px;padding:12px 14px;">'
            f'<div style="font-size:22px;font-weight:800;color:{INK};line-height:1.1;">{html.escape(value)}</div>'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:{MUTED};'
            f'margin-top:4px;">{html.escape(label)}</div></div></td>'
        )
    # rows of up to 4
    out_rows = []
    for i in range(0, len(cells_html), 4):
        chunk = cells_html[i:i + 4]
        out_rows.append("<tr>" + "".join(chunk) + "</tr>")
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;margin:14px 0;">{"".join(out_rows)}</table>'
    )


def progress_bars(block: str) -> str:
    """Progress meters. Each line: ``Label | current | target`` (optional
    ``| #color``). Shows a filled track with the percentage and current/target."""
    rows = _rows(block)
    out = []
    for i, cells in enumerate(rows):
        label = cells[0] if cells else ""
        current = _num(cells[1]) if len(cells) > 1 else 0.0
        target = _num(cells[2]) if len(cells) > 2 else 0.0
        color = cells[3] if len(cells) > 3 and cells[3].startswith("#") else SERIES[i % len(SERIES)]
        pct = 0 if target <= 0 else max(0, min(100, round(100 * current / target)))
        cur_txt = cells[1] if len(cells) > 1 else ""
        tgt_txt = cells[2] if len(cells) > 2 else ""
        track = (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse;table-layout:fixed;background:{TRACK};border-radius:6px;">'
            '<tr>'
            f'<td width="{max(1, pct)}%" style="background:{color};height:12px;line-height:12px;'
            'font-size:0;border-radius:6px;">&nbsp;</td>'
            f'<td style="font-size:0;line-height:12px;">&nbsp;</td></tr></table>'
        )
        out.append(
            '<div style="margin:12px 0;">'
            '<div style="font-size:13px;color:' + INK + ';margin-bottom:5px;">'
            f'<strong>{html.escape(label)}</strong> '
            f'<span style="color:{MUTED};">— {html.escape(cur_txt)} of {html.escape(tgt_txt)} '
            f'({pct}%)</span></div>'
            f'{track}</div>'
        )
    return "".join(out)


#: fence info-string -> renderer
RENDERERS = {
    "barchart": bar_chart,
    "bar": bar_chart,
    "stats": stat_cards,
    "statcards": stat_cards,
    "progress": progress_bars,
}


def render_block(kind: str, body: str) -> str | None:
    fn = RENDERERS.get((kind or "").strip().lower())
    return fn(body) if fn else None
