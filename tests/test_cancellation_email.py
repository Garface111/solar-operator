"""
Tests for send_cancellation_email — verifies the 30-day data-access window
is surfaced to the operator so they know to download before the purge runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _capture_resend(monkeypatch):
    sent: list[dict] = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    return sent


def test_cancellation_email_mentions_30_day_window(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_cancellation_email
    send_cancellation_email(to="op@example.com", name="Alice Operator")
    combined = sent[0]["html"] + sent[0]["text"]
    assert "30 days" in combined


def test_cancellation_email_mentions_permanent_deletion(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_cancellation_email
    send_cancellation_email(to="op@example.com", name="Alice Operator")
    combined = sent[0]["html"] + sent[0]["text"]
    assert "permanently deleted" in combined


def test_cancellation_email_shows_purge_date_from_cancel_date(monkeypatch):
    sent = _capture_resend(monkeypatch)
    from api.notify import send_cancellation_email
    cancel = datetime(2026, 6, 1)
    send_cancellation_email(to="op@example.com", name="Alice Operator",
                            cancel_date=cancel)
    combined = sent[0]["html"] + sent[0]["text"]
    # purge_date = June 1 + 30 days = July 1, 2026
    assert "July 1, 2026" in combined


def test_cancellation_email_purge_date_30_days_out(monkeypatch):
    """When no cancel_date supplied the purge date is ~30 days from now."""
    sent = _capture_resend(monkeypatch)
    from api.notify import send_cancellation_email
    before = datetime.utcnow()
    send_cancellation_email(to="op@example.com", name="Bob Operator")
    after = datetime.utcnow()
    combined = sent[0]["html"] + sent[0]["text"]
    # The purge month should be the month of (now + 30 days)
    expected_month = (before + timedelta(days=30)).strftime("%B")
    assert expected_month in combined
