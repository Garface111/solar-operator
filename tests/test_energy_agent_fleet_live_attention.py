"""EA fleet tools must match Spreadsheet NEED ATTENTION (14-day + live overlays)."""
from __future__ import annotations

from api.energy_agent import (
    _array_needs_attention,
    _column_live_anomalies,
    _summarize_column,
    _ui_live_verdict,
)


def _inv(name, power_w, nameplate_kw=10.0, status="ok", **extra):
    d = {
        "name": name,
        "current_power_w": power_w,
        "nameplate_kw": nameplate_kw,
        "status": status,
    }
    d.update(extra)
    return d


def test_live_verdict_dark_when_peers_produce():
    peers = [
        _inv("A", 5000),
        _inv("B", 5200),
        _inv("C", 0),  # dark
    ]
    assert _ui_live_verdict(peers[2], peers, True) == "dark"
    assert _ui_live_verdict(peers[0], peers, True) == "ok"


def test_live_verdict_low_vs_peers():
    # One unit at ~40% of nameplate while peers near 100%
    peers = [
        _inv("A", 9000, 10.0),
        _inv("B", 9500, 10.0),
        _inv("C", 9200, 10.0),
        _inv("D", 4000, 10.0),  # low
    ]
    assert _ui_live_verdict(peers[3], peers, True) == "low"


def test_night_suppresses_live_flags():
    peers = [_inv("A", 0), _inv("B", 0), _inv("C", 0)]
    assert _ui_live_verdict(peers[0], peers, False) == "ok"


def test_summarize_includes_live_and_needs_attention():
    col = {
        "array_name": "Chester 150kW",
        "vendor": "fronius",
        "is_daylight": True,
        "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
        "source_status": {"state": "ok", "age_hours": 0.1},
        "inverters": [
            _inv("1", 5000, 7.6),
            _inv("2", 5100, 7.6),
            _inv("3", 0, 7.6),
            _inv("4", 3000, 7.6),  # low if peers high
        ],
    }
    # Boost peers so #4 is low: need higher power peers
    col["inverters"] = [
        _inv("1", 7000, 7.6),
        _inv("2", 7100, 7.6),
        _inv("3", 0, 7.6),
        _inv("4", 3000, 7.6),
    ]
    assert _array_needs_attention(col) is True
    live = _column_live_anomalies(col)
    kinds = {x["live"] for x in live}
    assert "dark" in kinds
    row = _summarize_column(col)
    assert row["needs_attention"] is True
    assert row["live_dark_count"] >= 1
    assert "dark" in row["why"].lower() or "low" in row["why"].lower()
