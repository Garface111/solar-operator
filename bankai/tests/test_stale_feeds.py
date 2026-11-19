"""A feed that stops updating upstream must be detected and announced once —
pinned from Gaurav's checking sitting nine days stale in silence."""
import json
import time

import pytest

from bankai import config, scheduler
from bankai.connectors import simplefin
from bankai.db import session_scope
from bankai.models import MemoryNote


def _payload(days_stale_1971: float):
    now = time.time()
    return {
        "errors": [],
        "accounts": [
            {"id": "sf-2724", "name": "Adv Plus Banking- 2724", "balance": "561.57",
             "balance-date": now - 3600, "org": {"name": "BofA Ford"}, "transactions": []},
            {"id": "sf-1971", "name": "Adv Plus Banking- 1971", "balance": "9799.49",
             "balance-date": now - days_stale_1971 * 86400,
             "org": {"name": "Bank of America"}, "transactions": []},
        ],
    }


@pytest.fixture(autouse=True)
def one_bridge(monkeypatch):
    monkeypatch.setattr(config, "SIMPLEFIN_ACCESS_URLS", ["https://x/y"])
    monkeypatch.setattr(config, "STALE_FEED_DAYS", 3)
    # clear marker state between tests
    with session_scope() as s:
        for n in s.query(MemoryNote).filter_by(title=scheduler.STALE_FEED_MARKER):
            s.delete(n)


def test_a_stale_provider_timestamp_is_flagged(monkeypatch):
    monkeypatch.setattr(simplefin, "fetch", lambda url, days=90: _payload(9.0))
    out = simplefin.sync()
    assert out["status"] == "ok"
    stale = out["stale_feeds"]
    assert len(stale) == 1 and "1971" in stale[0]["account"]
    assert stale[0]["days_stale"] >= 8.9


def test_a_fresh_feed_is_not_flagged(monkeypatch):
    monkeypatch.setattr(simplefin, "fetch", lambda url, days=90: _payload(0.5))
    assert simplefin.sync()["stale_feeds"] == []


def test_provider_errors_are_surfaced(monkeypatch):
    payload = _payload(0.5)
    payload["errors"] = ["Connection to Bank of America may need attention"]
    monkeypatch.setattr(simplefin, "fetch", lambda url, days=90: payload)
    out = simplefin.sync()
    assert out["provider_errors"] == ["Connection to Bank of America may need attention"]


def test_the_household_is_alerted_once_per_episode(monkeypatch):
    sent = []
    from bankai.messaging import email_thread
    monkeypatch.setattr(email_thread, "configured", lambda: True)
    monkeypatch.setattr(
        email_thread, "start_thread",
        lambda s, subject, body: sent.append((subject, body)) or {"sent": True},
    )
    stale = {"stale_feeds": [
        {"account": "Adv Plus Banking- 1971", "institution": "Bank of America",
         "days_stale": 9.0}]}
    first = scheduler.alert_stale_feeds_once(stale)
    assert first["status"] == "emailed"
    assert "re-authentication" in sent[0][0]
    assert "bridge.simplefin.org" in sent[0][1]
    # the six-hourly loop must not nag again for the same episode
    second = scheduler.alert_stale_feeds_once(stale)
    assert second["status"] == "quiet" and len(sent) == 1
    # recovery clears the marker; a relapse re-alerts
    scheduler.alert_stale_feeds_once({"stale_feeds": []})
    third = scheduler.alert_stale_feeds_once(stale)
    assert third["status"] == "emailed" and len(sent) == 2