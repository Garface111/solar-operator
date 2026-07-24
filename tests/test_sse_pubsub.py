"""Unit tests for the rebuilt event backbone (api/events.py).

REBUILT 2026-07-24: publish() is now cross-process (pg_notify on Postgres,
direct local dispatch on SQLite) and delivery hops through the SSE loop via
call_soon_threadsafe. These tests cover the SQLite/local path: delivery,
tenant isolation, fanout, queue-full drop, and the /v1/sync emission. The
cross-process Postgres path is proven by the prod e2e probe (REBUILD-MAP §3).
"""
from __future__ import annotations

import asyncio
import json
import secrets
from unittest.mock import patch

from api.db import SessionLocal
from api.models import Tenant


def _make_tenant(*, active: bool = True) -> tuple[str, str]:
    """Insert a fresh tenant. Returns (tenant_id, tenant_key)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(18)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="SSE Test Co", contact_email="op@sse.test",
            tenant_key=key, plan="standard", active=active,
        ))
        db.commit()
    return tid, key


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(json.loads(q.get_nowait()))
    return out


# ─── local delivery unit tests (the SQLite transport path) ──────────────────

def test_publish_reaches_local_subscriber():
    from api import events

    async def _run():
        events._subscribers.clear()
        events._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        events._subscribers["ten_a"] = [q]

        events.publish("ten_a", "arrays.changed", {"array_id": 42})
        await asyncio.sleep(0)  # let call_soon_threadsafe land

        got = _drain(q)
        assert len(got) == 1
        assert got[0]["type"] == "arrays.changed"
        assert got[0]["tenant_id"] == "ten_a"
        assert got[0]["array_id"] == 42
        events._subscribers.clear()

    asyncio.run(_run())


def test_publish_tenant_isolation():
    from api import events

    async def _run():
        events._subscribers.clear()
        events._loop = asyncio.get_running_loop()
        q_a: asyncio.Queue = asyncio.Queue(maxsize=50)
        q_b: asyncio.Queue = asyncio.Queue(maxsize=50)
        events._subscribers["ten_a"] = [q_a]
        events._subscribers["ten_b"] = [q_b]

        events.publish("ten_a", "clients.changed", {})
        await asyncio.sleep(0)

        assert len(_drain(q_a)) == 1
        assert _drain(q_b) == []
        events._subscribers.clear()

    asyncio.run(_run())


def test_publish_fans_out_to_all_tenant_subscribers():
    from api import events

    async def _run():
        events._subscribers.clear()
        events._loop = asyncio.get_running_loop()
        q1: asyncio.Queue = asyncio.Queue(maxsize=50)
        q2: asyncio.Queue = asyncio.Queue(maxsize=50)
        events._subscribers["ten_c"] = [q1, q2]

        events.publish("ten_c", "generation.updated", {})
        await asyncio.sleep(0)

        assert len(_drain(q1)) == 1
        assert len(_drain(q2)) == 1
        events._subscribers.clear()

    asyncio.run(_run())


def test_delivery_drops_on_full_queue():
    """A slow consumer's full queue drops rather than blocks or raises."""
    from api import events

    async def _run():
        events._subscribers.clear()
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        events._subscribers["ten_d"] = [q]

        for i in range(3):  # third exceeds maxsize — must not raise
            events._deliver_local("ten_d", json.dumps({"type": "x", "i": i}))
        assert q.qsize() == 2
        events._subscribers.clear()

    asyncio.run(_run())


def test_publish_without_loop_is_safe():
    """Publishing with no SSE subscriber in this process (loop unset) must be
    a silent no-op, never an error — jobs publish from loopless threads."""
    from api import events
    events._subscribers.clear()
    events._loop = None
    events.publish("ten_e", "arrays.changed", {})  # must not raise


def test_broadcast_alias_delegates_to_publish():
    from api import events
    with patch.object(events, "publish", autospec=True) as pub:
        events.broadcast("ten_f", "capture.landed", {"client_id": 1})
    pub.assert_called_once_with("ten_f", "capture.landed", {"client_id": 1})


# ─── integration: /v1/sync emits capture.landed ─────────────────────────────

def test_sync_publishes_capture_landed(client):
    """POST /v1/sync that creates a new client publishes capture.landed with
    the new client id. (Transport-level delivery is unit-tested above; the
    cross-process path is proven live by the prod e2e probe.)"""
    from api import events

    tid, key = _make_tenant()
    payload = {
        "provider": "gmp",
        "user": {"email": "alice@example.com", "username": "alice"},
        "auth": {"apiToken": "tok_" + secrets.token_hex(8)},
        "accounts": [{
            "accountNumber": "ACC-SSE-001",
            "nickname": "Solar Home",
            "customerNumber": "cust-001",
            "serviceAddress": {"line1": "1 Main St"},
            "isPrimary": True,
            "solarNetMeter": True,
        }],
    }

    with patch.object(events, "publish", autospec=True) as pub:
        r = client.post("/v1/sync", json=payload,
                        headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["is_new_client"] is True

    landed = [c for c in pub.call_args_list
              if c.args[0] == tid and c.args[1] == "capture.landed"]
    assert landed, "capture.landed must be published for a new-client sync"
    assert landed[0].args[2]["is_new_client"] is True
    assert landed[0].args[2]["client_id"] is not None
