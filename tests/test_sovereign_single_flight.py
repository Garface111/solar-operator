"""Process-local heavy single-flight for Sovereign layers.

Ensures cortex/jobs/mission/skills cannot thrash Postgres together.
See api/sovereign_guard.py (try_begin_heavy / end_heavy / heavy_flight).
"""
from __future__ import annotations

import pytest

from api import sovereign_guard as guard


@pytest.fixture(autouse=True)
def _clean_flight(monkeypatch):
    """Reset flight state + default single-flight ON for every test."""
    monkeypatch.setenv("SOVEREIGN_SINGLE_FLIGHT", "1")
    guard._reset_flight_for_tests()
    yield
    guard._reset_flight_for_tests()
    monkeypatch.delenv("SOVEREIGN_SINGLE_FLIGHT", raising=False)


def test_begin_heavy_cortex_blocks_jobs():
    ok, reason = guard.try_begin_heavy("cortex")
    assert ok is True
    assert reason == "ok"

    ok2, reason2 = guard.try_begin_heavy("jobs")
    assert ok2 is False
    assert "single_flight" in reason2
    assert "held_by=cortex" in reason2

    st = guard.flight_status()
    assert st["busy"] is True
    assert st["held_by"] == "cortex"
    assert st["held_since"] is not None


def test_end_heavy_releases_for_jobs():
    ok, _ = guard.try_begin_heavy("cortex")
    assert ok
    guard.end_heavy("cortex")

    ok2, reason2 = guard.try_begin_heavy("jobs")
    assert ok2 is True
    assert reason2 == "ok"
    assert guard.flight_status()["held_by"] == "jobs"
    guard.end_heavy("jobs")
    assert guard.flight_status()["busy"] is False


def test_single_flight_disabled_allows_concurrent(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_SINGLE_FLIGHT", "0")
    ok1, _ = guard.try_begin_heavy("cortex")
    ok2, reason2 = guard.try_begin_heavy("jobs")
    assert ok1 is True
    assert ok2 is True
    assert reason2 == "ok"
    # When disabled, state is not tracked as busy
    assert guard.flight_status()["enabled"] is False
    assert guard.flight_status()["busy"] is False


def test_non_reentrant_same_layer_fails():
    ok1, _ = guard.try_begin_heavy("cortex")
    assert ok1 is True
    ok2, reason2 = guard.try_begin_heavy("cortex")
    assert ok2 is False
    assert "held_by=cortex" in reason2


def test_end_heavy_wrong_layer_noop():
    guard.try_begin_heavy("cortex")
    guard.end_heavy("jobs")  # wrong holder — must not release
    assert guard.flight_status()["held_by"] == "cortex"
    guard.end_heavy("cortex")
    assert guard.flight_status()["busy"] is False


def test_heavy_flight_context_manager():
    with guard.heavy_flight("mission_loop") as (ok, reason):
        assert ok is True
        assert reason == "ok"
        ok2, reason2 = guard.try_begin_heavy("skills")
        assert ok2 is False
        assert "held_by=mission_loop" in reason2
    # Released after exit
    assert guard.flight_status()["busy"] is False
    with guard.heavy_flight("skills") as (ok3, _):
        assert ok3 is True


def test_heavy_flight_yields_false_when_busy():
    guard.try_begin_heavy("jobs")
    with guard.heavy_flight("cortex") as (ok, reason):
        assert ok is False
        assert "single_flight" in reason
    # Original holder still owns it
    assert guard.flight_status()["held_by"] == "jobs"
    guard.end_heavy("jobs")


def test_guard_status_includes_single_flight():
    guard.try_begin_heavy("cortex_wake")
    st = guard.guard_status()
    assert "single_flight" in st
    sf = st["single_flight"]
    assert sf["held_by"] == "cortex_wake"
    assert sf["busy"] is True
    assert sf["enabled"] is True
    assert st["rules"]["single_flight_enabled"] is True
    guard.end_heavy("cortex_wake")
