"""The phone door: parse the tunnel URL, and only bother the household when it
actually changes."""
from bankai import tunnel


BANNER = """
2026-08-12T01:30:00Z INF Thank you for trying Cloudflare Tunnel.
+--------------------------------------------------------+
|  https://spare-violet-mango-tree.trycloudflare.com      |
+--------------------------------------------------------+
2026-08-12T01:30:01Z INF Registered tunnel connection
"""


def test_parses_the_url_out_of_the_banner():
    assert tunnel.parse_url(BANNER) == "https://spare-violet-mango-tree.trycloudflare.com"
    assert tunnel.parse_url("no url here") is None
    assert tunnel.parse_url("") is None


def test_remember_only_reports_a_real_change(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel, "URL_FILE", tmp_path / "tunnel-url")
    assert tunnel.remember("https://a.trycloudflare.com") is True
    assert tunnel.remember("https://a.trycloudflare.com") is False, (
        "an unchanged URL must not email the household on every restart"
    )
    assert tunnel.remember("https://b.trycloudflare.com") is True
    assert tunnel.current_url() == "https://b.trycloudflare.com"


def test_announce_emails_both_spouses(session, monkeypatch):
    from bankai.messaging import email_thread

    seen = {}
    monkeypatch.setattr(email_thread, "configured", lambda: True)
    monkeypatch.setattr(email_thread, "start_thread",
                        lambda s, subject, body: seen.update(subject=subject, body=body))
    out = tunnel.announce("https://c.trycloudflare.com")
    assert out["emailed"] is True
    assert "portal" in seen["subject"].lower()
    assert "https://c.trycloudflare.com" in seen["body"]
    # the old link silently 404ing is the confusing failure; say so
    assert "stopped working" in seen["body"]


def test_announce_falls_back_to_the_thread_when_email_is_dark(session, monkeypatch):
    from bankai.db import session_scope
    from bankai.messaging import email_thread
    from bankai.models import ChatMessage

    monkeypatch.setattr(email_thread, "configured", lambda: False)
    out = tunnel.announce("https://d.trycloudflare.com")
    assert out["emailed"] is False
    with session_scope() as s:
        said = [m.content for m in s.query(ChatMessage).all()]
    assert any("d.trycloudflare.com" in c for c in said)


# --- the portal on a phone -------------------------------------------------
# Reached over the tunnel, the dashboard is a phone page now. These pin the two
# CSS facts that were actually broken/needed, so a later restyle can't quietly
# bring the sideways scroll back.

def test_portal_layout_cannot_side_scroll_on_a_phone():
    from bankai import config

    html = (config.BASE_DIR / "bankai" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # A bare `1fr` track is floored at min-content, so the widest table pushed
    # the column past the viewport (measured: 103px of overflow at 375px).
    assert "grid-template-columns:minmax(0,1fr)" in html
    assert "@media (max-width: 720px)" in html
    # Wide tables scroll inside themselves instead of dragging the page along.
    assert "overflow-x:auto" in html
    # iOS zooms the page when a focused input is under 16px.
    assert "font-size:16px" in html


def test_scrollbars_are_styled_not_default():
    """Ford's note: the default bars are 'really ugly and the wrong color'.
    They are a bright square slab on a dark panel, and no amount of other
    polish survives them sitting down the side of every list."""
    from bankai import config

    html = (config.BASE_DIR / "bankai" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "::-webkit-scrollbar-thumb" in html          # Chrome/Safari/Edge
    assert "scrollbar-color" in html                    # Firefox
    assert "background-clip:content-box" in html        # slim thumb, not welded to the edge
    assert "::-webkit-scrollbar-corner" in html         # the grey square where the two bars meet


def test_the_interface_is_rendered_with_care():
    """A handful of details that separate 'styled' from 'made': crisp text on a
    dark ground, money that doesn't jitter as digits change, and motion that
    stops when the OS asks for stillness."""
    from bankai import config

    html = (config.BASE_DIR / "bankai" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "-webkit-font-smoothing:antialiased" in html
    assert "font-variant-numeric:tabular-nums" in html
    assert "prefers-reduced-motion" in html
    assert "transition:none !important" in html
