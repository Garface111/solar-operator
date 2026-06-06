"""
Unit tests for the in-process SSE pub/sub (api/events.py).

Tests run synchronously via asyncio.run() — no pytest-asyncio required.
Covers tenant isolation, multi-subscriber fanout, queue-full drop, and the
integration path where /v1/sync broadcasts after a new client is created.
"""
from __future__ import annotations

import asyncio
import json
import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant


# ─── helpers ──────────────────────────────────────────────────────────────────

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


# ─── pub/sub unit tests ────────────────────────────────────────────────────────

def test_broadcast_reaches_own_subscriber():
    """broadcast() delivers the event payload to the subscriber queue."""
    from api import events

    async def _run():
        events._subscribers.clear()
        q = await events._subscribe("ten_a")
        events.broadcast("ten_a", "capture.landed", {"client_id": 42, "is_new_client": True})
        assert not q.empty()
        raw = q.get_nowait()
        data = json.loads(raw)
        assert data["type"] == "capture.landed"
        assert data["client_id"] == 42
        assert data["is_new_client"] is True
        await events._unsubscribe("ten_a", q)
        events._subscribers.clear()

    asyncio.run(_run())


def test_broadcast_tenant_isolation():
    """broadcast() for tenant A does NOT reach tenant B's subscriber."""
    from api import events

    async def _run():
        events._subscribers.clear()
        q_a = await events._subscribe("ten_a")
        q_b = await events._subscribe("ten_b")

        events.broadcast("ten_a", "capture.landed", {"client_id": 1})

        assert not q_a.empty()
        assert q_b.empty()

        await events._unsubscribe("ten_a", q_a)
        await events._unsubscribe("ten_b", q_b)
        events._subscribers.clear()

    asyncio.run(_run())


def test_broadcast_multiple_subscribers_same_tenant():
    """All subscribers for the same tenant receive the event."""
    from api import events

    async def _run():
        events._subscribers.clear()
        q1 = await events._subscribe("ten_c")
        q2 = await events._subscribe("ten_c")

        events.broadcast("ten_c", "capture.landed", {"client_id": 7})

        assert not q1.empty()
        assert not q2.empty()

        data1 = json.loads(q1.get_nowait())
        data2 = json.loads(q2.get_nowait())
        assert data1["client_id"] == 7
        assert data2["client_id"] == 7

        await events._unsubscribe("ten_c", q1)
        await events._unsubscribe("ten_c", q2)
        events._subscribers.clear()

    asyncio.run(_run())


def test_broadcast_drops_on_full_queue():
    """broadcast() silently drops when the subscriber queue is at capacity."""
    from api import events

    async def _run():
        events._subscribers.clear()
        q = asyncio.Queue(maxsize=2)
        events._subscribers["ten_d"] = [q]

        events.broadcast("ten_d", "capture.landed", {"client_id": 1})
        events.broadcast("ten_d", "capture.landed", {"client_id": 2})
        # Third event exceeds maxsize=2 — must not raise
        events.broadcast("ten_d", "capture.landed", {"client_id": 3})

        assert q.qsize() == 2
        events._subscribers.pop("ten_d", None)

    asyncio.run(_run())


def test_unsubscribe_cleans_up_empty_tenant():
    """_unsubscribe() removes the tenant key when the last subscriber leaves."""
    from api import events

    async def _run():
        events._subscribers.clear()
        q = await events._subscribe("ten_e")
        assert "ten_e" in events._subscribers
        await events._unsubscribe("ten_e", q)
        assert "ten_e" not in events._subscribers

    asyncio.run(_run())


# ─── integration: /v1/sync broadcasts after new-client creation ────────────────

def test_sync_broadcasts_capture_landed(client):
    """POST /v1/sync that creates a new client should broadcast capture.landed."""
    from api import events

    tid, key = _make_tenant()

    async def _setup():
        events._subscribers.clear()
        return await events._subscribe(tid)

    q = asyncio.run(_setup())

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

    r = client.post("/v1/sync", json=payload, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_new_client"] is True

    # The broadcast put an item in the queue
    assert not q.empty(), "capture.landed event should have been broadcast"
    raw = q.get_nowait()
    data = json.loads(raw)
    assert data["type"] == "capture.landed"
    assert data["client_id"] is not None
    assert data["is_new_client"] is True

    asyncio.run(events._unsubscribe(tid, q))
    events._subscribers.clear()
