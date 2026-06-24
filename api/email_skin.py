"""
Product-aware email skin — shared HTML/text wrapper for all outbound emails.

Two brands share one backend (see models.Tenant.product / api.branding):

  • NEPOOL Operator (nepooloperator.com) — LIGHT "solarpunk" theme pulled straight
    from the live site tokens: warm cream, white cards, emerald-700, and the hero's
    sky→mint wash on the header (was a heavy dark-emerald block).
      page #f4f1e4 · card #ffffff · header mint #dcebda (sky→mint→cream gradient)
      emerald accent/CTA #047857 · warm ink #2a2520 · wordmark strip #064e3b

  • Array Operator (arrayoperator.com) — LIGHT "day" theme (utility blue on
    cool slate), mirroring theme-day.css + the morning fleet digest.
      page #f6f8fb · card #ffffff · blue accent #2563eb · CTA #2563eb
      ink #0f172a · slate wordmark strip #0f172a

Pass product="array_operator" to render in the AO theme; anything else (incl.
None) renders NEPOOL. Both themes are LIGHT and the skin forces light mode
(color-scheme: light only) so dark-mode clients can't auto-invert them; bgcolor
attributes sit alongside style backgrounds for Outlook's Word engine.

Layout (unchanged across themes):
  ┌────────────────────────────────────────┐
  │  brand header  ─ accent hairline ─     │   wordmark + quiet tagline
  ├────────────────────────────────────────┤
  │  email body (caller-supplied HTML)     │
  │  [ optional CTA button ]               │
  │  [ optional attachment chip ]          │
  ├────────────────────────────────────────┤
  │  small muted footer line               │
  │  dark wordmark strip (brand · domain)  │
  └────────────────────────────────────────┘
"""
from __future__ import annotations

# Font names use SINGLE quotes — these strings are interpolated into
# double-quoted style="..." attributes, so double-quoted font names
# ("Segoe UI") would prematurely close the attribute and silently drop every
# declaration after font-family. Single quotes are valid CSS and attribute-safe.
_FONT = "-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"

_ARRAY = "array_operator"


# ── Per-product design tokens ────────────────────────────────────────────────
_THEMES = {
    "nepool": {
        # Pulled from the live nepooloperator.com tokens so the email reads as the
        # SAME solarpunk-light brand: warm cream page, white cards, emerald-700
        # primary, and the hero's sky→mint→cream wash on the header (vs the old
        # heavy dark-emerald block). header_gradient is progressive enhancement —
        # Gmail/Apple render the wash, Outlook falls back to the solid mint bgcolor.
        "page_bg": "#f4f1e4",         # site --bg (warm cream, NOT pure white)
        "card_bg": "#ffffff",
        "card_border": "#e5ddd0",     # site --border
        "header_bg": "#dcebda",       # solarpunk mint (hero mid-band) — solid fallback
        "header_gradient": "linear-gradient(180deg,#cfe6ef 0%,#d8ebd9 46%,#e7f1e1 100%)",
        "header_text": "#0d3b2e",     # deep emerald ink, readable on mint
        "header_sub": "#3f6b56",      # muted emerald
        "accent": "#047857",          # emerald hairline under the header (site --primary)
        "body_text": "#2a2520",       # site --fg (warm ink)
        "muted_text": "#6b5e55",      # site --fg-muted
        "footer_border": "#e5ddd0",
        "cta_bg": "#047857",          # site --primary / .btn-primary
        "cta_text": "#ffffff",
        "wordmark_bg": "#c3e6cb",     # soft light-green footer (matches the mint header family)
        "wordmark_text": "#0d3b2e",   # deep emerald ink, readable on the light strip
        "chip_bg": "#fbf8f0",         # site .btn-secondary cream
        "chip_border": "#e5ddd0",
        "chip_icon_bg": "#047857",
        "chip_icon_text": "#ffffff",
        "link": "#047857",
        "brand": "NEPOOL Operator",
        "wordmark": "NEPOOL Operator · nepooloperator.com",
        # HTML-only wordmark so the domain link is on-brand emerald (not the
        # client's default-blue autolink) on the light footer. Plain `wordmark`
        # above stays for the text/plain fallback (render_email_skin_text).
        "wordmark_html": ('NEPOOL Operator · <a href="https://nepooloperator.com" '
                          'style="color:#065f46;text-decoration:none;font-weight:600;">'
                          'nepooloperator.com</a>'),
        "default_tagline": "Quarterly NEPOOL-GIS generation reports, made simple.",
        "footer_default": "Sent by NEPOOL Operator — solar accounting for the rest of us.",
        "chip_caption": "NEPOOL-GIS generation workbook",
    },
    "array_operator": {
        # Array Operator DAY skin -- light "utility blue on cool slate", matching
        # theme-day.css + the morning fleet digest. (Was a dark navy/green skin.)
        "page_bg": "#f6f8fb",
        "card_bg": "#ffffff",
        "card_border": "#e2e8f0",
        "header_bg": "#ffffff",
        "header_text": "#0f172a",
        "header_sub": "#64748b",
        "accent": "#2563eb",          # utility-blue hairline under the header
        "body_text": "#0f172a",
        "muted_text": "#64748b",
        "footer_border": "#eef2f7",
        "cta_bg": "#2563eb",
        "cta_text": "#ffffff",
        "wordmark_bg": "#f8fafc",
        "wordmark_text": "#64748b",
        "chip_bg": "#f8fafc",
        "chip_border": "#e2e8f0",
        "chip_icon_bg": "#2563eb",
        "chip_icon_text": "#ffffff",
        "link": "#2563eb",
        "brand": "Array Operator",
        "wordmark": "Array Operator · arrayoperator.com",
        "default_tagline": "Your array, measured at its true worth — watched, valued, in dollars.",
        "footer_default": "Sent by Array Operator — an agent watching every panel for you.",
        "chip_caption": "Array performance report",
    },
}


def _theme(product: str | None) -> dict:
    return _THEMES[_ARRAY] if (product or "") == _ARRAY else _THEMES["nepool"]


def link_color(product: str | None) -> str:
    """Accent color for inline <a> links inside body_html, so callers can match
    the brand (NEPOOL emerald vs AO green) without hard-coding hex."""
    return _theme(product)["link"]


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
    headline: str | None = None,
    intro_line: str,
    body_html: str,
    cta: dict | None = None,
    footer_line: str | None = None,
    wordmark: str | None = None,
    attachment_label: str | None = None,
    attachment_size_bytes: int | None = None,
    attachment_caption: str | None = None,
    product: str | None = "nepool",
) -> str:
    """Return a complete HTML email wrapped in the product's brand design.

    product: "array_operator" → AO dark theme; anything else → NEPOOL light.
    preheader: hidden inbox-preview line (15-90 chars).
    headline: bold leading line in the header strip. Defaults to the brand name.
    intro_line: quiet tagline under the headline (do NOT pass the subject here).
    body_html: SAFE HTML — caller escapes any operator-supplied content.
    cta: optional {label, url} for a single brand button.
    footer_line: small line above the wordmark strip (defaults to brand tagline).
    attachment_label / _size_bytes / _caption: optional attachment chip.
    """
    t = _theme(product)
    _headline = headline or t["brand"]
    _footer = footer_line or t["footer_default"]
    _tagline = (intro_line or "").strip() or t["default_tagline"]

    _cta_block = ""
    if cta:
        _cta_block = (
            '\n<table cellpadding="0" cellspacing="0" border="0" role="presentation"'
            ' style="margin-top:24px;">'
            "<tr><td"
            f' bgcolor="{t["cta_bg"]}"'
            f' style="background:{t["cta_bg"]};border-radius:6px;mso-padding-alt:12px 22px;">'
            f'<a href="{cta["url"]}"'
            f' style="display:inline-block;padding:12px 22px;color:{t["cta_text"]};'
            f"text-decoration:none;font-family:{_FONT};font-size:14px;"
            f'font-weight:600;border-radius:6px;letter-spacing:.01em;">'
            f'{cta["label"]}</a></td></tr></table>'
        )

    _attachment_block = ""
    if attachment_label:
        size = _human_filesize(attachment_size_bytes)
        size_html = (
            f'<span style="color:{t["muted_text"]};font-weight:400;margin-left:8px;">· {size}</span>'
            if size else ""
        )
        caption = attachment_caption or t["chip_caption"]
        _attachment_block = (
            '\n<table cellpadding="0" cellspacing="0" border="0" width="100%" role="presentation"'
            ' style="margin-top:28px;border-collapse:separate;">'
            '<tr><td'
            f' bgcolor="{t["chip_bg"]}"'
            f' style="background:{t["chip_bg"]};border:1px solid {t["chip_border"]};border-radius:8px;'
            'padding:14px 18px;">'
            f'<table cellpadding="0" cellspacing="0" border="0" role="presentation" width="100%">'
            f'<tr>'
            f'<td width="32" valign="middle" style="padding-right:12px;">'
            f'<div style="width:32px;height:32px;border-radius:6px;background:{t["chip_icon_bg"]};'
            f'color:{t["chip_icon_text"]};font-family:{_FONT};font-size:14px;font-weight:600;'
            f'text-align:center;line-height:32px;">XLS</div>'
            f'</td>'
            f'<td valign="middle">'
            f'<div style="font-family:{_FONT};font-size:13px;font-weight:600;color:{t["body_text"]};">'
            f'{attachment_label}{size_html}</div>'
            f'<div style="font-family:{_FONT};font-size:11px;color:{t["muted_text"]};margin-top:2px;">'
            f'Attached · {caption}</div>'
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
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<style>:root{{color-scheme:light only;supported-color-schemes:light;}}</style>
<title>{_headline}</title>
</head>
<body style="margin:0;padding:0;background:{t["page_bg"]};color-scheme:light only;" bgcolor="{t["page_bg"]}">
<span style="display:none;font-size:0;line-height:0;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{preheader}</span>
<table cellpadding="0" cellspacing="0" border="0" width="100%" role="presentation"
  bgcolor="{t["page_bg"]}" style="background:{t["page_bg"]};padding:36px 0;">
  <tr><td align="center">
    <table cellpadding="0" cellspacing="0" border="0" width="580" role="presentation"
      bgcolor="{t["card_bg"]}" style="max-width:580px;width:100%;background:{t["card_bg"]};border:1px solid {t["card_border"]};border-radius:10px;box-shadow:0 1px 2px rgba(0,0,0,0.04);">
      <tr><td bgcolor="{t["header_bg"]}" style="background:{t["header_bg"]};background:{t.get("header_gradient") or t["header_bg"]};padding:28px 36px;border-radius:10px 10px 0 0;border-bottom:3px solid {t["accent"]};">
        <div style="font-family:{_FONT};font-size:22px;font-weight:600;color:{t["header_text"]};line-height:1.25;letter-spacing:-0.01em;">{_headline}</div>
        <div style="font-family:{_FONT};font-size:13px;color:{t["header_sub"]};margin-top:8px;line-height:1.45;">{_tagline}</div>
      </td></tr>
      <tr><td bgcolor="{t["card_bg"]}" style="background:{t["card_bg"]};padding:32px 36px 8px 36px;font-family:{_FONT};font-size:15px;line-height:1.65;color:{t["body_text"]};">
{body_html}{_cta_block}{_attachment_block}
      </td></tr>
      <tr><td bgcolor="{t["card_bg"]}" style="background:{t["card_bg"]};padding:20px 36px 24px 36px;font-family:{_FONT};font-size:12px;color:{t["muted_text"]};line-height:1.5;border-top:1px solid {t["footer_border"]};">
{_footer}
      </td></tr>
      <tr><td bgcolor="{t["wordmark_bg"]}" style="background:{t["wordmark_bg"]};padding:14px 36px;font-family:{_FONT};font-size:11px;color:{t["wordmark_text"]};text-align:center;border-radius:0 0 10px 10px;letter-spacing:0.04em;">
{wordmark if wordmark is not None else (t.get("wordmark_html") or t["wordmark"])}
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def render_email_skin_text(
    *,
    headline: str | None = None,
    intro_line: str,
    body_text: str,
    cta: dict | None = None,
    attachment_label: str | None = None,
    wordmark: str | None = None,
    product: str | None = "nepool",
) -> str:
    """Return a clean plain-text fallback. Headline in ALL CAPS, CTA as label: url."""
    t = _theme(product)
    parts = [
        (headline or t["brand"]).upper(),
        (intro_line or "").strip() or t["default_tagline"],
        "",
        body_text,
    ]
    if cta:
        parts += ["", f"{cta['label']}: {cta['url']}"]
    if attachment_label:
        parts += ["", f"📎 Attached: {attachment_label}"]
    parts += ["", "—", wordmark if wordmark is not None else t["wordmark"]]
    return "\n".join(parts)
