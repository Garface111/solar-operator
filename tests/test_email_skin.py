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
    assert "Solar Operator · solaroperator.org" in html


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
    assert "Solar Operator · solaroperator.org" in html
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
    assert "Solar Operator · solaroperator.org" in text


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
    assert "Solar Operator · solaroperator.org" in html
    assert "mso-hide:all" in html

    text = captured["text"]
    assert text is not None
    assert "SOLAR OPERATOR" in text
    assert "Solar Operator · solaroperator.org" in text
