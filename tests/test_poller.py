"""Tests for the data-hub poller + the daylight honesty gate."""
import types
from datetime import timedelta

import api.inverter_fleet as fleet
from api.inverter_fleet import _live_power_w, _POWER_FRESH
from api.models import Inverter, now


def _inv(pw, age_hours):
    iv = Inverter(tenant_id="t", array_id=1, vendor="sma", serial="X")
    iv.last_power_w = pw
    iv.last_power_at = now() - timedelta(hours=age_hours)
    return iv


def test_live_value_always_trusted():
    # A freshly-pulled live telemetry value is returned regardless of daylight.
    iv = _inv(None, 0)
    assert _live_power_w(iv, {"last_power_w": 5000.0}, daylight=False) == 5000.0
    assert _live_power_w(iv, {"last_power_w": 5000.0}, daylight=True) == 5000.0


def test_stale_capture_fresh_daytime_shows():
    # Captured reading, 2h old, daytime, within freshness window → shown.
    iv = _inv(17000.0, 2)
    assert _live_power_w(iv, {}, daylight=True) == 17000.0


def test_stale_capture_at_night_is_hidden():
    # THE BUG FIX: a 2pm capture must NOT read as producing at night.
    iv = _inv(17000.0, 6)
    assert _live_power_w(iv, {}, daylight=False) is None


def test_capture_beyond_freshness_window_hidden():
    iv = _inv(17000.0, _POWER_FRESH.total_seconds() / 3600 + 1)
    assert _live_power_w(iv, {}, daylight=True) is None


def test_poller_skips_when_dark(monkeypatch):
    from api import poller
    monkeypatch.setattr(poller._fleet, "_is_daylight", lambda: False)
    summary = poller.poll_all_sources()
    assert summary["ran"] is False
    assert summary["daylight"] is False


def test_pullable_connection_requires_creds():
    from api import poller
    # api_key + site_id → pullable
    c1 = types.SimpleNamespace(config={"api_key": "k", "site_id": 1}, vendor="solaredge")
    # oauth creds → pullable
    c2 = types.SimpleNamespace(config={"refresh_token": "r"}, vendor="sma")
    # bare reading, no creds → not pullable
    c3 = types.SimpleNamespace(config={}, vendor="fronius")

    def fake_resolve(db, arr):
        return arr._conn
    from api import poller as P
    P._fleet._resolve_connection = fake_resolve  # monkeypatch direct
    assert P._pullable_connection(None, types.SimpleNamespace(_conn=c1)) is c1
    assert P._pullable_connection(None, types.SimpleNamespace(_conn=c2)) is c2
    assert P._pullable_connection(None, types.SimpleNamespace(_conn=c3)) is None
    assert P._pullable_connection(None, types.SimpleNamespace(_conn=None)) is None
