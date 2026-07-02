"""
Tests for api/email_skin.py — solarpunk design token presence and structure.

Re-baselined 2026-07-01 to the CURRENT product-aware skin (commits 43634c9 →
319a681). The NEPOOL theme was redesigned from the old dark-emerald + gold-
underline block to a solarpunk-LIGHT palette lifted from the live site tokens:
  page  #f4f1e4  (warm cream, was #faf8f5)
  header #dcebda  (mint hero band, was dark #064e3b)
  accent #047857  (emerald hairline under the header, replaced gold #e6b470)
  wordmark strip #c3e6cb / ink #0d3b2e  (soft light-green, was dark #022c22)
The wordmark is now HTML-wrapped (an <a> around the domain) so the brand text
and domain appear as separate substrings, not one contiguous string, in HTML.
Array Operator renders its own light "day" (utility-blue) theme — see the AO
tests below. Do NOT revert the skin to satisfy these; the design is intentional.
"""
from __future__ import annotations

from api.email_skin import render_email_skin, render_email_skin_text

# Current NEPOOL (default) design tokens — kept in one place so a future skin
# refresh updates the tests by editing here, not hunting inline hex literals.
NEPOOL_PAGE_BG = "#f4f1e4"
NEPOOL_HEADER_BG = "#dcebda"
NEPOOL_ACCENT = "#047857"       # emerald hairline under the header (was gold)
NEPOOL_WORDMARK_BG = "#c3e6cb"
NEPOOL_WORDMARK_INK = "#0d3b2e"
# Old dark tokens that must NOT reappear (guards against an accidental revert).
NEPOOL_OLD_DARK = ("#064e3b", "#e6b470", "#faf8f5", "#022c22", "#e8e2d9")


def _assert_nepool_wordmark(blob: str) -> None:
    """The NEPOOL wordmark strip carries the brand + domain. In HTML the domain
    is wrapped in an <a>, so the two halves are separate substrings; in plain
    text it's one contiguous line."""
    assert "NEPOOL Operator ·" in blob
    assert "nepooloperator.com" in blob


# ── render_email_skin ─────────────────────────────────────────────────────────

def test_skin_contains_header_bg_color():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert NEPOOL_HEADER_BG in html, "NEPOOL mint header bg missing"


def test_skin_contains_accent_hairline():
    """The NEPOOL header now carries an emerald accent hairline (the old gold
    underline #e6b470 was dropped in the solarpunk-light redesign)."""
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert NEPOOL_ACCENT in html, "emerald accent hairline missing"
    assert "#e6b470" not in html, "old gold underline should be gone"


def test_skin_contains_page_bg():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert NEPOOL_PAGE_BG in html, "NEPOOL cream page bg missing"


def test_skin_contains_wordmark_footer():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    _assert_nepool_wordmark(html)


def test_skin_contains_preheader_mso_hide():
    html = render_email_skin(
        preheader="inbox preview text", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert "mso-hide:all" in html
    assert "inbox preview text" in html


def test_skin_contains_body_html():
    html = render_email_skin(
        preheader="p", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert "<p>Z</p>" in html


def test_skin_cta_button_rendered():
    html = render_email_skin(
        preheader="p", headline="X", intro_line="Y", body_html="<p>body</p>",
        cta={"label": "Click here", "url": "https://example.com/action"},
    )
    assert "Click here" in html
    assert "https://example.com/action" in html
    assert "mso-padding-alt" in html  # CTA button padding present
    assert NEPOOL_ACCENT in html      # CTA button background is the emerald accent


def test_skin_no_cta_when_not_provided():
    html = render_email_skin(
        preheader="p", headline="X", intro_line="Y", body_html="<p>body</p>",
    )
    # Emerald #047857 is now a structural brand color (header hairline + links),
    # so its presence no longer signals a CTA. The CTA <table> is the only place
    # that emits the Outlook `mso-padding-alt` button padding — use that as the
    # discriminator instead.
    assert "mso-padding-alt" not in html  # no CTA button when none provided


def test_skin_custom_footer_line():
    html = render_email_skin(
        preheader="p", headline="X", intro_line="Y", body_html="<p>b</p>",
        footer_line="Custom footer text here.",
    )
    assert "Custom footer text here." in html


def test_skin_long_body_does_not_break():
    long_body = "<p>" + ("A" * 5000) + "</p>"
    html = render_email_skin(
        preheader="long body test", headline="X", intro_line="Y",
        body_html=long_body,
    )
    assert len(html) > 5000
    assert NEPOOL_HEADER_BG in html
    assert NEPOOL_ACCENT in html
    assert NEPOOL_PAGE_BG in html
    _assert_nepool_wordmark(html)
    assert "mso-hide:all" in html


# ── render_email_skin_text ────────────────────────────────────────────────────

def test_text_skin_headline_is_uppercase():
    text = render_email_skin_text(
        headline="Solar Operator",
        intro_line="Welcome aboard.",
        body_text="Here is the body.",
    )
    assert "SOLAR OPERATOR" in text
    assert "Solar Operator\n" not in text  # not mixed-case on its own line


def test_text_skin_contains_wordmark():
    text = render_email_skin_text(
        headline="H", intro_line="I", body_text="Body."
    )
    assert "NEPOOL Operator · nepooloperator.com" in text  # rebranded from "Solar Operator · solaroperator.org"


def test_text_skin_cta_rendered_as_label_url():
    text = render_email_skin_text(
        headline="H",
        intro_line="I",
        body_text="Body.",
        cta={"label": "Open dashboard", "url": "https://solaroperator.org/accounts"},
    )
    assert "Open dashboard: https://solaroperator.org/accounts" in text


def test_text_skin_multiline():
    text = render_email_skin_text(
        headline="H", intro_line="I", body_text="Line one.\n\nLine two."
    )
    lines = text.splitlines()
    assert len(lines) > 3  # headline + intro + blank + body + footer


# ── Smoke: send_welcome_email assembles branded HTML ─────────────────────────

def test_send_welcome_smoke():
    """send_welcome_email must assemble HTML containing all core brand colors."""
    import api.notify as notify
    from unittest.mock import patch

    captured: dict = {}

    def _capture(to, subject, html, text=None, **kwargs):
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(notify, "_send_via_resend", side_effect=_capture):
        notify.send_welcome_email("x@test.com", "Alice Test", "key_abc", "standard")

    html = captured["html"]
    for color in (NEPOOL_PAGE_BG, NEPOOL_HEADER_BG, NEPOOL_ACCENT,
                  NEPOOL_WORDMARK_BG, NEPOOL_WORDMARK_INK):
        assert color in html, f"Brand color {color} missing from welcome email HTML"
    for old in NEPOOL_OLD_DARK:
        assert old not in html, f"old dark token {old} leaked into welcome email"
    _assert_nepool_wordmark(html)
    assert "mso-hide:all" in html

    text = captured["text"]
    assert text is not None
    assert "NEPOOL OPERATOR" in text  # rebranded from "SOLAR OPERATOR"
    assert "NEPOOL Operator · nepooloperator.com" in text  # plain-text wordmark is contiguous


# ── Array Operator (light "day") theme ────────────────────────────────────────

def test_ao_theme_uses_light_day_palette():
    html = render_email_skin(
        preheader="p", intro_line="Y", body_html="<p>Z</p>",
        product="array_operator",
    )
    # AO day tokens present (utility blue on cool slate); old dark tokens gone.
    for c in ("#f6f8fb", "#ffffff", "#2563eb"):
        assert c in html, f"AO day color {c} missing"
    for c in ("#0a0e14", "#11161f", "#3fd68a", "#7ff0bb"):
        assert c not in html, f"old AO dark color {c} should be gone"
    assert "#faf8f5" not in html  # not the NEPOOL cream page bg
    assert "light only" in html   # forces light so clients can't invert it
    assert "Array Operator · arrayoperator.com" in html
    assert "NEPOOL Operator · nepooloperator.com" not in html


def test_ao_theme_default_headline_is_brand():
    html = render_email_skin(
        preheader="p", intro_line="Y", body_html="<p>Z</p>",
        product="array_operator",
    )
    assert "Array Operator" in html  # headline falls back to brand


def test_ao_cta_uses_blue():
    html = render_email_skin(
        preheader="p", intro_line="Y", body_html="<p>b</p>",
        cta={"label": "Sign in", "url": "https://arrayoperator.com/login?token=x"},
        product="array_operator",
    )
    assert "#2563eb" in html       # utility-blue CTA bg
    assert "#ffffff" in html       # white CTA text
    assert "#3fd68a" not in html   # no leftover octarine-green


def test_nepool_remains_default_and_light():
    html = render_email_skin(preheader="p", intro_line="Y", body_html="<p>Z</p>")
    assert NEPOOL_PAGE_BG in html          # warm cream page (solarpunk-light)
    _assert_nepool_wordmark(html)
    assert "#0a0e14" not in html           # no dark AO tokens
    assert "light only" in html            # NEPOOL is force-light too


def test_text_skin_is_product_aware():
    text = render_email_skin_text(
        intro_line="Welcome.", body_text="hello", product="array_operator",
    )
    assert "ARRAY OPERATOR" in text
    assert "Array Operator · arrayoperator.com" in text
