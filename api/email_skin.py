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
"""
from __future__ import annotations

_FONT = '-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif'


def render_email_skin(
    *,
    preheader: str,
    headline: str,
    intro_line: str,
    body_html: str,
    cta: dict | None = None,
    footer_line: str | None = None,
) -> str:
    """Return a complete HTML email wrapped in Solar Operator's design.

    preheader: hidden inbox-preview line (15-90 chars).
    headline: bold leading line shown in the emerald header strip.
    intro_line: smaller subhead under the headline.
    body_html: SAFE HTML — caller is responsible for escaping operator-supplied content.
    cta: optional {label: str, url: str} for a single button (deep emerald).
    footer_line: small line above the wordmark strip (defaults to quiet tagline).
    """
    _footer = footer_line or "Solar Operator · solaroperator.org"

    _cta_block = ""
    if cta:
        _cta_block = (
            '\n<table cellpadding="0" cellspacing="0" border="0" role="presentation"'
            ' style="margin-top:24px;">'
            "<tr><td"
            ' style="background:#047857;border-radius:4px;mso-padding-alt:12px 22px;">'
            f'<a href="{cta["url"]}"'
            f' style="display:inline-block;padding:12px 22px;color:#ffffff;'
            f"text-decoration:none;font-family:{_FONT};font-size:14px;"
            f'font-weight:600;border-radius:4px;">'
            f'{cta["label"]}</a></td></tr></table>'
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
  style="background:#faf8f5;padding:30px 0;">
  <tr><td align="center">
    <table cellpadding="0" cellspacing="0" border="0" width="580" role="presentation"
      style="max-width:580px;width:100%;background:#ffffff;border:1px solid #e8e2d9;border-radius:6px;">
      <tr><td style="background:#064e3b;padding:24px 32px;border-radius:6px 6px 0 0;border-bottom:3px solid #e6b470;">
        <div style="font-family:{_FONT};font-size:20px;font-weight:600;color:#ffffff;line-height:1.3;">{headline}</div>
        <div style="font-family:{_FONT};font-size:13px;color:#d1fae5;margin-top:6px;line-height:1.4;">{intro_line}</div>
      </td></tr>
      <tr><td style="padding:28px 32px;font-family:{_FONT};font-size:15px;line-height:1.6;color:#1a2a1f;">
{body_html}{_cta_block}
      </td></tr>
      <tr><td style="padding:4px 32px 16px 32px;font-family:{_FONT};font-size:13px;color:#3a5a42;">
{_footer}
      </td></tr>
      <tr><td style="background:#022c22;padding:12px 32px;font-family:{_FONT};font-size:11px;color:#cfe4d3;text-align:center;border-radius:0 0 6px 6px;">
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
) -> str:
    """Return a clean plain-text fallback. Headline in ALL CAPS, CTA as label: url."""
    parts = [
        headline.upper(),
        intro_line,
        "",
        body_text,
    ]
    if cta:
        parts += ["", f"{cta['label']}: {cta['url']}"]
    parts += ["", "—", "Solar Operator · solaroperator.org"]
    return "\n".join(parts)
