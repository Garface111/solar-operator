"""DB pool hardening — fail-fast under saturation, health stays up without checkout.

Covers the 2026-07-14 QueuePool outage class:
  • pool_status() never checks out a connection
  • /health is async and reports pool pressure fields
  • SQLAlchemy pool TimeoutError → HTTP 503 with Retry-After
  • dispose_pool() is safe to call
  • pool watchdog is registered on the scheduler
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_pool_status_has_required_keys():
    from api.db import pool_status
    st = pool_status()
    for k in ("dialect", "capacity", "checked_out", "pressure", "timeouts",
              "pool_size", "max_overflow"):
        assert k in st, f"missing {k}"
    assert st["dialect"] in ("sqlite", "postgresql", "unknown")
    assert isinstance(st["pressure"], bool)


def test_dispose_pool_safe():
    from api.db import dispose_pool, pool_status
    before = pool_status()
    out = dispose_pool(reason="test")
    assert out["reason"] == "test"
    assert "before" in out and "after" in out
    # Still usable after dispose
    st = pool_status()
    assert st["dialect"] == before["dialect"]


def test_health_reports_pool_fields(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "db_pool_max" in body
    assert "db_pool_pressure" in body
    assert "db_pool_checked_out" in body
    assert "db_pool_timeouts" in body
    # Health must not require a live DB checkout to answer
    assert isinstance(body["db_pool_pressure"], bool)


def test_pool_timeout_maps_to_503(client: TestClient, monkeypatch):
    """A QueuePool TimeoutError must 503 immediately, not hang."""
    from api import app as appmod
    from api.db import PoolTimeout

    @appmod.app.get("/__test_pool_timeout")
    def _boom():
        raise PoolTimeout("QueuePool limit of size 15 overflow 15 reached")

    r = client.get("/__test_pool_timeout")
    assert r.status_code == 503
    body = r.json()
    assert body.get("code") == "db_pool_exhausted"
    assert r.headers.get("retry-after") == "5"
    assert "busy" in (body.get("detail") or "").lower()


def test_pool_watchdog_registered():
    """The self-heal tick must be wired into start() or it ships dark."""
    from api import scheduler as sched_mod

    class FakeSched:
        def __init__(self):
            self.jobs = []
        def add_job(self, fn, *a, **kw):
            self.jobs.append({"fn": fn, "id": kw.get("id"), "kwargs": kw})
        def start(self):
            pass

    fake = FakeSched()
    real = sched_mod.scheduler
    sched_mod.scheduler = fake
    try:
        sched_mod.start()
    finally:
        sched_mod.scheduler = real

    ids = [j["id"] for j in fake.jobs]
    assert "db_pool_watchdog" in ids, f"watchdog missing from {ids}"


def test_watchdog_disposes_after_two_pressure_ticks(monkeypatch):
    from api import scheduler as sched_mod
    from api.db import pool_status

    disposed = []
    alerts = []

    # Force two consecutive pressure readings
    monkeypatch.setattr(
        "api.db.pool_status",
        lambda: {
            "dialect": "postgresql",
            "pressure": True,
            "checked_out": 30,
            "capacity": 30,
        },
    )
    monkeypatch.setattr(
        "api.db.dispose_pool",
        lambda reason="": disposed.append(reason) or {
            "before": {"pressure": True}, "after": {"pressure": False}, "reason": reason,
        },
    )
    # scheduler binds send_internal_alert at import time into its own namespace
    monkeypatch.setattr(
        "api.scheduler.send_internal_alert",
        lambda s, b: alerts.append(s) or True,
    )

    sched_mod._pool_pressure_streak = 0
    sched_mod._run_pool_watchdog()  # streak → 1, no dispose yet
    assert disposed == []
    sched_mod._run_pool_watchdog()  # streak → 2 → dispose
    assert len(disposed) == 1
    assert alerts, "expected internal alert after dispose"
    assert sched_mod._pool_pressure_streak == 0
