"""SolarEdge naive-local timestamps must be converted to UTC using the site's
timezone, not assumed UTC. Regression for the 'GMP says 24h, we say 19h' bug
where a VT site's local last_report was mis-read as UTC (~4h too stale)."""
from datetime import datetime, timezone

from api.adapters import solaredge as se


def test_localize_local_to_utc():
    # 02:39 EDT (America/New_York, UTC-4 in June) -> 06:39 UTC.
    iso = se._localize_to_utc_iso("2026-06-19 02:39:51", "America/New_York")
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).isoformat() == "2026-06-19T06:39:51+00:00"


def test_localize_no_tz_leaves_naive():
    # Unknown tz → leave naive (downstream treats naive as UTC = pre-fix behavior).
    iso = se._localize_to_utc_iso("2026-06-19 02:39:51", None)
    assert iso == "2026-06-19T02:39:51"
    assert datetime.fromisoformat(iso).tzinfo is None


def test_localize_none_passthrough():
    assert se._localize_to_utc_iso(None, "America/New_York") is None


def test_site_timezone_cached(monkeypatch):
    calls = {"n": 0}

    class _R:
        is_success = True
        def json(self):
            return {"details": {"location": {"timeZone": "America/New_York"}}}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _R()

    monkeypatch.setattr(se.httpx, "get", fake_get)
    se._SITE_TZ_CACHE.pop(999, None)
    assert se._site_timezone("k", 999) == "America/New_York"
    assert se._site_timezone("k", 999) == "America/New_York"
    assert calls["n"] == 1   # second call served from cache
