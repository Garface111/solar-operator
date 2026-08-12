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
