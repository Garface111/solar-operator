"""Turn the copilot's markdown into a clean, well-formatted HTML email.

The copilot already writes in markdown — headings, **bold**, bullet lists, and
especially the pipe tables it uses for payment calendars. Sent as plain text
those render as literal ``**`` and ``|`` noise, which is why the emails looked
bad. This renders that same markdown into a styled, email-client-safe HTML
document (inline styles, because Gmail and friends strip <style> blocks), while
always keeping the original text as the plaintext alternative.

Security: every bit of source text is HTML-escaped BEFORE any markup is applied,
so only the tags this module generates are ever real HTML. A transaction memo or
document snippet that contains ``<script>`` becomes inert text, never markup —
the same untrusted-content discipline the rest of the system holds.
"""
from __future__ import annotations

import html
import re

from . import charts

# --- palette (inlined into every element; email clients ignore <style>) ---
INK = "#1a2b3c"     # body text
MUTED = "#6b7a8b"   # secondary text
ACCENT = "#0f6b57"  # deep green header + links
LINE = "#e4e9ef"    # borders / rules
BG = "#f4f6f9"      # page background
CARD = "#ffffff"    # message card

#: GitHub-style callout panels: a blockquote whose first line is [!TYPE].
#: (border, tint background, label colour, label text)
CALLOUTS = {
    "NOTE": ("#3b82f6", "#eff6ff", "#1d4ed8", "Note"),
    "INFO": ("#3b82f6", "#eff6ff", "#1d4ed8", "Note"),
    "TIP": ("#0f6b57", "#f0f7f4", "#0f6b57", "Tip"),
    "SUCCESS": ("#0f6b57", "#f0f7f4", "#0f6b57", "Done"),
    "IMPORTANT": ("#7c3aed", "#f5f3ff", "#6d28d9", "Important"),
    "WARNING": ("#d97706", "#fffbeb", "#b45309", "Heads up"),
    "CAUTION": ("#dc2626", "#fef2f2", "#b91c1c", "Caution"),
}
_CALLOUT_RE = re.compile(r"^\s*\[!(\w+)\]\s*(.*)$")

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\*_])[*_]([^*_\n]+)[*_](?![\*_])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _inline(text: str) -> str:
    """Inline markdown on an ALREADY HTML-escaped string."""
    # Protect inline code first so nothing formats inside it.
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)
    text = _LINK.sub(
        rf'<a href="\2" style="color:{ACCENT};text-decoration:underline;">\1</a>',
        text,
    )
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    def restore(m: re.Match) -> str:
        code = codes[int(m.group(1))]
        return (
            '<code style="background:#eef2f6;border-radius:4px;padding:1px 5px;'
            'font-family:ui-monospace,Menlo,Consolas,monospace;font-size:90%;">'
            f"{code}</code>"
        )

    return re.sub(r"\x00(\d+)\x00", restore, text)


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line)) and "-" in line


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _render_table(rows: list[str]) -> str:
    header = _cells(rows[0])
    body = [_cells(r) for r in rows[2:]]
    th = "".join(
        f'<th style="text-align:left;padding:8px 12px;border-bottom:2px solid {LINE};'
        f'font-size:13px;color:{MUTED};text-transform:uppercase;letter-spacing:.03em;">'
        f"{_inline(html.escape(c))}</th>"
        for c in header
    )
    trs = []
    for i, r in enumerate(body):
        bg = CARD if i % 2 == 0 else "#fafbfc"
        tds = "".join(
            f'<td style="padding:8px 12px;border-bottom:1px solid {LINE};'
            f'font-size:14px;color:{INK};vertical-align:top;">{_inline(html.escape(c))}</td>'
            for c in r
        )
        trs.append(f'<tr style="background:{bg};">{tds}</tr>')
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" '
        'style="width:100%;border-collapse:collapse;margin:14px 0;">'
        f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    )


def _render_blocks(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # fenced block: ```barchart / ```stats / ```progress render as charts;
        # any other ``` fence renders as a monospace code block.
        if stripped.startswith("```"):
            kind = stripped[3:].strip()
            body_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body_lines.append(lines[i])
                i += 1
            i += 1  # consume the closing fence
            chart = charts.render_block(kind, "\n".join(body_lines))
            if chart is not None:
                out.append(chart)
            else:
                code = html.escape("\n".join(body_lines))
                out.append(
                    '<pre style="margin:12px 0;padding:12px 14px;background:#f5f7fa;'
                    'border:1px solid #e4e9ef;border-radius:8px;overflow-x:auto;'
                    'font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;'
                    f'line-height:1.5;color:{INK};">{code}</pre>'
                )
            continue

        # horizontal rule
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line) and not _is_table_sep(line):
            out.append(f'<hr style="border:none;border-top:1px solid {LINE};margin:22px 0;">')
            i += 1
            continue

        # heading
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            size = {1: 22, 2: 18, 3: 15, 4: 14}[level]
            top = 22 if level <= 2 else 16
            out.append(
                f'<h{level} style="margin:{top}px 0 8px;font-size:{size}px;line-height:1.3;'
                f'color:{INK};font-weight:700;">{_inline(html.escape(m.group(2).strip()))}</h{level}>'
            )
            i += 1
            continue

        # table: a pipe line followed by a separator line
        if "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
            rows = [line, lines[i + 1]]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(_render_table(rows))
            continue

        # blockquote — or a colored callout panel when it opens with [!TYPE]
        if stripped.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            m_call = _CALLOUT_RE.match(quote[0]) if quote else None
            if m_call and m_call.group(1).upper() in CALLOUTS:
                border, bg, label_color, label = CALLOUTS[m_call.group(1).upper()]
                first = m_call.group(2).strip()
                rest = [first, *quote[1:]] if first else quote[1:]
                body_html = "<br>".join(_inline(html.escape(p)) for p in rest if p is not None)
                out.append(
                    f'<div style="margin:16px 0;padding:12px 16px;border-left:4px solid {border};'
                    f'background:{bg};border-radius:0 8px 8px 0;">'
                    f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:.05em;color:{label_color};margin-bottom:4px;">{label}</div>'
                    f'<div style="font-size:14px;line-height:1.55;color:{INK};">{body_html}</div>'
                    f"</div>"
                )
                continue
            out.append(
                f'<blockquote style="margin:14px 0;padding:8px 16px;border-left:3px solid {ACCENT};'
                f'background:#f0f7f4;color:{INK};font-size:14px;">'
                f"{_inline(html.escape(' '.join(quote)))}</blockquote>"
            )
            continue

        # unordered list
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            lis = "".join(
                f'<li style="margin:4px 0;font-size:14px;line-height:1.55;color:{INK};">'
                f"{_inline(html.escape(it))}</li>"
                for it in items
            )
            out.append(f'<ul style="margin:10px 0;padding-left:22px;">{lis}</ul>')
            continue

        # ordered list
        if re.match(r"^\s*\d+[.)]\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))
                i += 1
            lis = "".join(
                f'<li style="margin:4px 0;font-size:14px;line-height:1.55;color:{INK};">'
                f"{_inline(html.escape(it))}</li>"
                for it in items
            )
            out.append(f'<ol style="margin:10px 0;padding-left:22px;">{lis}</ol>')
            continue

        # paragraph: gather until a blank line or a block starter
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^\s*(#{1,4}\s|[-*]\s|\d+[.)]\s|>)", lines[i]
        ) and not ("|" in lines[i] and _is_table_sep(lines[i] if i < n else "")):
            para.append(lines[i])
            i += 1
        joined = "<br>".join(_inline(html.escape(p)) for p in para)
        out.append(
            f'<p style="margin:10px 0;font-size:14px;line-height:1.6;color:{INK};">{joined}</p>'
        )
    return "\n".join(out)


def render_email(subject: str, body_markdown: str, *, preheader: str = "") -> str:
    """A full, styled HTML email document from the copilot's markdown body."""
    content = _render_blocks(body_markdown or "")
    safe_subject = html.escape((subject or "Your household copilot").strip())
    pre = html.escape(preheader or "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
</head>
<body style="margin:0;padding:0;background:{BG};">
<span style="display:none;max-height:0;overflow:hidden;opacity:0;">{pre}</span>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:640px;max-width:100%;background:{CARD};border:1px solid {LINE};border-radius:14px;overflow:hidden;">
<tr><td style="background:{ACCENT};padding:18px 28px;">
<div style="font-size:16px;font-weight:700;color:#ffffff;letter-spacing:.01em;">BankAI</div>
<div style="font-size:12px;color:#cdebe0;margin-top:2px;">your household copilot</div>
</td></tr>
<tr><td style="padding:26px 28px 12px;">
<div style="font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:{MUTED};margin-bottom:2px;">{safe_subject}</div>
{content}
</td></tr>
<tr><td style="padding:14px 28px 24px;border-top:1px solid {LINE};">
<div style="font-size:12px;color:{MUTED};line-height:1.5;">Sent by your household copilot to Ford &amp; Gaurav. Reply to this email to talk to me — I read every message.</div>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""
