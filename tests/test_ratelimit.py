"""Unit tests for the in-process sliding-window rate limiter (api/ratelimit.py).

We test allow() directly (deterministic, no clock sleeps) because enforce()
no-ops under pytest so the suite's shared client IP can't trip real limits.
"""
from __future__ import annotations

import api.ratelimit as rl


def setup_function(_):
    rl._HITS.clear()


def test_allows_up_to_limit_then_blocks():
    for i in range(5):
        assert rl.allow("b", "k", max_hits=5, window_s=100) is True, f"hit {i} should pass"
    # 6th within the window is blocked.
    assert rl.allow("b", "k", max_hits=5, window_s=100) is False


def test_keys_are_independent():
    for _ in range(5):
        assert rl.allow("b", "alice", max_hits=5, window_s=100)
    assert rl.allow("b", "alice", max_hits=5, window_s=100) is False
    # A different key has its own budget.
    assert rl.allow("b", "bob", max_hits=5, window_s=100) is True


def test_buckets_are_independent():
    assert rl.allow("login", "k", max_hits=1, window_s=100) is True
    assert rl.allow("login", "k", max_hits=1, window_s=100) is False
    # Same key, different bucket → separate budget.
    assert rl.allow("signup", "k", max_hits=1, window_s=100) is True


def test_window_expiry_frees_budget(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: t["now"])
    assert rl.allow("b", "k", max_hits=2, window_s=10)
    assert rl.allow("b", "k", max_hits=2, window_s=10)
    assert rl.allow("b", "k", max_hits=2, window_s=10) is False  # full
    t["now"] += 11  # slide past the window
    assert rl.allow("b", "k", max_hits=2, window_s=10) is True   # freed


def test_enforce_raises_429_when_over(monkeypatch):
    # Force the pytest-bypass off so we can exercise enforce() end to end.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import fastapi

    class _Req:
        headers = {"x-forwarded-for": "9.9.9.9"}
        client = type("C", (), {"host": "9.9.9.9"})()

    rl.enforce(_Req(), "e", max_hits=1, window_s=100)
    with __import__("pytest").raises(fastapi.HTTPException) as exc:
        rl.enforce(_Req(), "e", max_hits=1, window_s=100)
    assert exc.value.status_code == 429
