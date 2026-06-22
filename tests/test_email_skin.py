"""
Tests for api/email_skin.py — solarpunk design token presence and structure.
"""
from __future__ import annotations

from api.email_skin import render_email_skin, render_email_skin_text


# ── render_email_skin ─────────────────────────────────────────────────────────

def test_skin_contains_header_bg_color():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert "#064e3b" in html, "header bg #064e3b missing"


def test_skin_contains_gold_underline():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert "#e6b470" in html, "gold underline #e6b470 missing"


def test_skin_contains_page_bg():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert "#faf8f5" in html, "page bg #faf8f5 missing"


def test_skin_contains_wordmark_footer():
    html = render_email_skin(
        preheader="test pre", headline="X", intro_line="Y", body_html="<p>Z</p>"
    )
    assert "NEPOOL Operator · nepooloperator.com" in html  # rebranded from "Solar Operator · solaroperator.org"


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
    assert "#047857" in html  # CTA button background


def test_skin_no_cta_when_not_provided():
    html = render_email_skin(
        preheader="p", headline="X", intro_line="Y", body_html="<p>body</p>",
    )
    # No anchor tag pointing to an action URL
    assert "#047857" not in html  # CTA bg should not appear without CTA


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
    assert "#064e3b" in html
    assert "#e6b470" in html
    assert "#faf8f5" in html
    assert "NEPOOL Operator · nepooloperator.com" in html  # rebranded from "Solar Operator · solaroperator.org"
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
    for color in ("#064e3b", "#e6b470", "#faf8f5", "#e8e2d9", "#022c22"):
        assert color in html, f"Brand color {color} missing from welcome email HTML"
    assert "NEPOOL Operator · nepooloperator.com" in html  # rebranded from "Solar Operator · solaroperator.org"
    assert "mso-hide:all" in html

    text = captured["text"]
    assert text is not None
    assert "NEPOOL OPERATOR" in text  # rebranded from "SOLAR OPERATOR"
    assert "NEPOOL Operator · nepooloperator.com" in text  # rebranded from "Solar Operator · solaroperator.org"


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
    assert "#faf8f5" in html
    assert "NEPOOL Operator · nepooloperator.com" in html
    assert "#0a0e14" not in html
    assert "light only" in html   # NEPOOL is force-light too


def test_text_skin_is_product_aware():
    text = render_email_skin_text(
        intro_line="Welcome.", body_text="hello", product="array_operator",
    )
    assert "ARRAY OPERATOR" in text
    assert "Array Operator · arrayoperator.com" in text
