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


def test_enforce_tenant_caps_one_account(monkeypatch):
    """The metered-model cap keys on tenant, not IP.

    api/energy_agent.py had no limiter at all while hosting every paid-model
    endpoint, so one signed-in account could drive unbounded Anthropic/xAI/OpenAI
    spend (Ford 2026-07-25). IP is the wrong key there -- shared behind NAT,
    trivially rotated, and the bill follows the account.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import fastapi
    import pytest

    for _ in range(3):
        rl.enforce_tenant("ten_a", "ea_chat", max_hits=3, window_s=300)
    with pytest.raises(fastapi.HTTPException) as exc:
        rl.enforce_tenant("ten_a", "ea_chat", max_hits=3, window_s=300)
    assert exc.value.status_code == 429


def test_enforce_tenant_isolates_accounts(monkeypatch):
    """One tenant burning its budget must not throttle everyone else."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import fastapi
    import pytest

    for _ in range(2):
        rl.enforce_tenant("ten_noisy", "ea_chat", max_hits=2, window_s=300)
    with pytest.raises(fastapi.HTTPException):
        rl.enforce_tenant("ten_noisy", "ea_chat", max_hits=2, window_s=300)
    # a different account is untouched
    rl.enforce_tenant("ten_quiet", "ea_chat", max_hits=2, window_s=300)


def test_enforce_tenant_never_blocks_without_a_tenant(monkeypatch):
    """A missing tenant id must not 429 -- fail open, never lock a user out."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for _ in range(50):
        rl.enforce_tenant(None, "ea_chat", max_hits=1, window_s=300)
    for _ in range(50):
        rl.enforce_tenant("", "ea_chat", max_hits=1, window_s=300)
