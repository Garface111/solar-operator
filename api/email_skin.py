"""
Solar Operator email skin — shared HTML/text wrapper for all outbound emails.

Design tokens mirror web/app/tailwind.config.js:
  primary-700  #064e3b  — header strip bg
  primary-500  #047857  — CTA button bg
  primary-100  #d1fae5  — header intro text
  cream        #faf8f5  — page bg
  cream-border #e8e2d9  — card border hairline
  wood-300     #e6b470  — gold underline accent (the solarpunk touch)
  text-body    #1a2a1f  — body copy
  text-muted   #3a5a42  — secondary / footer copy
  primary-950  #022c22  — wordmark strip bg

Layout (Jun 8 2026 redesign):
  ┌────────────────────────────────────────┐
  │  emerald header                        │   wordmark + tagline (no duplication
  │  ─ gold hairline ─                     │   of subject — that's already in the
  │                                        │   inbox preview)
  ├────────────────────────────────────────┤
  │                                        │
  │  operator's rendered email body        │   plain prose, the way they wrote it
  │                                        │
  ├────────────────────────────────────────┤
  │  ┌──────────────────────────────────┐  │   ← OPTIONAL attachment chip:
  │  │ 📎  filename.xlsx  · size kb     │  │     surfaced visually so the
  │  └──────────────────────────────────┘  │     workbook isn't lost in the
  │                                        │     attachment dropdown.
  ├────────────────────────────────────────┤
  │  small muted tagline                   │
  ├────────────────────────────────────────┤
  │  dark wordmark strip (single copy)     │
  └────────────────────────────────────────┘
"""
from __future__ import annotations

_FONT = '-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif'

# The tagline that sits as a quiet subhead in the emerald header strip.
# Static + brand-y — not a per-email value — so it never collides with the
# subject text the inbox already shows alongside the From row.
_DEFAULT_TAGLINE = "Quarterly NEPOOL-GIS generation reports, made simple."


def _human_filesize(num_bytes: int | None) -> str:
    """Render a file size as 'NN KB' / 'N.N MB'. Returns '' on falsy input."""
    if not num_bytes or num_bytes <= 0:
        return ""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    return f"{mb:.1f} MB"


def render_email_skin(
    *,
    preheader: str,
    headline: str,
    intro_line: str,
    body_html: str,
    cta: dict | None = None,
    footer_line: str | None = None,
    attachment_label: str | None = None,
    attachment_size_bytes: int | None = None,
) -> str:
    """Return a complete HTML email wrapped in Solar Operator's design.

    preheader: hidden inbox-preview line (15-90 chars).
    headline: bold leading line in the emerald header strip — usually "Solar Operator".
    intro_line: the quiet brand tagline under the headline.  Kept generic; do
        NOT pass the email subject here (the subject is already visible in
        the inbox row and shoehorning it under the headline reads as a glitch).
    body_html: SAFE HTML — caller is responsible for escaping operator-supplied content.
    cta: optional {label: str, url: str} for a single button (deep emerald).
    footer_line: small line above the wordmark strip (defaults to quiet tagline).
    attachment_label: optional filename, e.g. "Catamount-GMCS-report.xlsx". When
        provided, an attachment chip is rendered between the body and footer
        so the recipient sees the file even if their client hides attachments.
    attachment_size_bytes: optional, surfaced as a small muted suffix.
    """
    _footer = footer_line or "Sent by Solar Operator — solar accounting for the rest of us."
    _tagline = (intro_line or "").strip() or _DEFAULT_TAGLINE

    _cta_block = ""
    if cta:
        _cta_block = (
            '\n<table cellpadding="0" cellspacing="0" border="0" role="presentation"'
            ' style="margin-top:24px;">'
            "<tr><td"
            ' style="background:#047857;border-radius:6px;mso-padding-alt:12px 22px;">'
            f'<a href="{cta["url"]}"'
            f' style="display:inline-block;padding:12px 22px;color:#ffffff;'
            f"text-decoration:none;font-family:{_FONT};font-size:14px;"
            f'font-weight:600;border-radius:6px;letter-spacing:.01em;">'
            f'{cta["label"]}</a></td></tr></table>'
        )

    _attachment_block = ""
    if attachment_label:
        size = _human_filesize(attachment_size_bytes)
        size_html = (
            f'<span style="color:#6b7c6e;font-weight:400;margin-left:8px;">· {size}</span>'
            if size else ""
        )
        _attachment_block = (
            '\n<table cellpadding="0" cellspacing="0" border="0" width="100%" role="presentation"'
            ' style="margin-top:28px;border-collapse:separate;">'
            '<tr><td'
            ' style="background:#f6f1e9;border:1px solid #e8e2d9;border-radius:8px;'
            'padding:14px 18px;">'
            f'<table cellpadding="0" cellspacing="0" border="0" role="presentation" width="100%">'
            f'<tr>'
            f'<td width="32" valign="middle" style="padding-right:12px;">'
            f'<div style="width:32px;height:32px;border-radius:6px;background:#047857;'
            f'color:#ffffff;font-family:{_FONT};font-size:14px;font-weight:600;'
            f'text-align:center;line-height:32px;">XLS</div>'
            f'</td>'
            f'<td valign="middle">'
            f'<div style="font-family:{_FONT};font-size:13px;font-weight:600;color:#1a2a1f;">'
            f'{attachment_label}{size_html}</div>'
            f'<div style="font-family:{_FONT};font-size:11px;color:#3a5a42;margin-top:2px;">'
            f'Attached · NEPOOL-GIS generation workbook</div>'
            f'</td>'
            f'</tr></table>'
            '</td></tr></table>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>Solar Operator</title>
</head>
<body style="margin:0;padding:0;background:#faf8f5;">
<span style="display:none;font-size:0;line-height:0;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{preheader}</span>
<table cellpadding="0" cellspacing="0" border="0" width="100%" role="presentation"
  style="background:#faf8f5;padding:36px 0;">
  <tr><td align="center">
    <table cellpadding="0" cellspacing="0" border="0" width="580" role="presentation"
      style="max-width:580px;width:100%;background:#ffffff;border:1px solid #e8e2d9;border-radius:10px;box-shadow:0 1px 2px rgba(0,0,0,0.04);">
      <tr><td style="background:#064e3b;padding:28px 36px;border-radius:10px 10px 0 0;border-bottom:3px solid #e6b470;">
        <div style="font-family:{_FONT};font-size:22px;font-weight:600;color:#ffffff;line-height:1.25;letter-spacing:-0.01em;">{headline}</div>
        <div style="font-family:{_FONT};font-size:13px;color:#d1fae5;margin-top:8px;line-height:1.45;">{_tagline}</div>
      </td></tr>
      <tr><td style="padding:32px 36px 8px 36px;font-family:{_FONT};font-size:15px;line-height:1.65;color:#1a2a1f;">
{body_html}{_cta_block}{_attachment_block}
      </td></tr>
      <tr><td style="padding:20px 36px 24px 36px;font-family:{_FONT};font-size:12px;color:#6b7c6e;line-height:1.5;border-top:1px solid #f0ead9;">
{_footer}
      </td></tr>
      <tr><td style="background:#022c22;padding:14px 36px;font-family:{_FONT};font-size:11px;color:#cfe4d3;text-align:center;border-radius:0 0 10px 10px;letter-spacing:0.04em;">
Solar Operator · solaroperator.org
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def render_email_skin_text(
    *,
    headline: str,
    intro_line: str,
    body_text: str,
    cta: dict | None = None,
    attachment_label: str | None = None,
) -> str:
    """Return a clean plain-text fallback. Headline in ALL CAPS, CTA as label: url."""
    parts = [
        headline.upper(),
        (intro_line or "").strip() or _DEFAULT_TAGLINE,
        "",
        body_text,
    ]
    if cta:
        parts += ["", f"{cta['label']}: {cta['url']}"]
    if attachment_label:
        parts += ["", f"📎 Attached: {attachment_label}"]
    parts += ["", "—", "Solar Operator · solaroperator.org"]
    return "\n".join(parts)
