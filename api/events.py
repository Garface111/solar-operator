"""Tenant-scoped event backbone: publish anywhere, hear everywhere.

REBUILT 2026-07-24 (see C:\\Users\\fordg\\CC\\genrep-rebuild\\REBUILD-MAP.md).
The old version was a per-process dict of asyncio queues — an event emitted in
uvicorn worker A never reached an SSE client on worker B (~50% loss at the
default 2 workers), and the scheduler/worker SERVICE could not publish at all.
Combined with exactly one emission site in the whole backend (capture.landed),
"nothing updates unless you reload" was the designed-in behavior.

Now: Postgres LISTEN/NOTIFY is the transport.

  publish(tenant_id, type, payload)   — callable from ANY process or thread
      (web workers, the worker service, admin scripts, daemon threads).
      Postgres: pg_notify('so_events', json) via the engine pool. SQLite
      (tests/dev): direct local delivery. ALWAYS call AFTER your commit —
      subscribers reload state on receipt, so an event that beats its own
      transaction reads stale and never hears a correction.

  /v1/events (SSE)                    — per-process LISTEN thread (lazy-started
      on the first subscriber) relays notifications into that process's local
      asyncio queues via call_soon_threadsafe. Cross-worker and cross-service
      by construction.

EVENT VOCABULARY (coarse, reload-triggering — surfaces refetch their own
queries on receipt; these are not state deltas):
  clients.changed     a client was created/updated/merged/retired
  arrays.changed      arrays created/attached/moved/deleted, GIS ids assigned
  generation.updated  DailyGeneration rows landed (pull, bill→daily, backfill)
  job.updated         a background job changed status (payload: kind, status)
  capture.landed      extension/cloud capture created or updated a client

Payloads stay small (NOTIFY caps at ~8KB): tenant_id + optional client_id /
array_id / kind / status. Never put customer data in an event.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from .account import tenant_from_session
from .db import engine as db_engine

log = logging.getLogger(__name__)
router = APIRouter()

CHANNEL = "so_events"
HEARTBEAT_SECS = 20

# tenant_id → list of active SSE subscriber queues (one per open connection),
# local to THIS process. The LISTEN thread is what makes them globally fed.
_subscribers: dict[str, list[asyncio.Queue]] = {}

_listener_lock = threading.Lock()
_listener_started = False
# The asyncio loop serving SSE in this process — captured when the first
# subscriber connects, used by non-loop threads to deliver safely.
_loop: asyncio.AbstractEventLoop | None = None


def _is_postgres() -> bool:
    try:
        return db_engine.dialect.name == "postgresql"
    except Exception:  # noqa: BLE001 — never let transport detection crash a caller
        return False


# ─── publishing ─────────────────────────────────────────────────────────────


def publish(tenant_id: str, event_type: str, payload: dict | None = None) -> None:
    """Emit a tenant event. Fire-and-forget; safe from any thread or process.

    Call AFTER commit. Failures are logged, never raised — an event must never
    break the mutation it announces.
    """
    body = {"type": event_type, "tenant_id": tenant_id, **(payload or {})}
    data = json.dumps(body)
    try:
        if _is_postgres():
            with db_engine.connect() as conn:
                conn.exec_driver_sql(
                    "SELECT pg_notify(%(ch)s, %(body)s)",
                    {"ch": CHANNEL, "body": data},
                )
                conn.commit()
        else:
            _dispatch_raw(data)
    except Exception:  # noqa: BLE001
        log.warning("event publish failed: %s %s", tenant_id, event_type,
                    exc_info=True)


def broadcast(tenant_id: str, event_type: str, payload: dict) -> None:
    """Back-compat name for the original capture.landed call site. Same
    contract as publish(), now cross-process."""
    publish(tenant_id, event_type, payload)


# ─── delivery (LISTEN thread → local asyncio queues) ────────────────────────


def _deliver_local(tenant_id: str, data: str) -> None:
    """Runs ON the asyncio loop. Fan a serialized event out to this process's
    subscribers for the tenant. Slow consumers drop rather than block."""
    for q in list(_subscribers.get(tenant_id, [])):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


def _dispatch_raw(payload: str) -> None:
    """Route one serialized event toward local subscribers, from any thread."""
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return
    tenant_id = obj.get("tenant_id")
    loop = _loop
    if not tenant_id or loop is None or loop.is_closed():
        return  # nobody can hear in this process — fine, others may
    loop.call_soon_threadsafe(_deliver_local, tenant_id, payload)


def _listen_forever() -> None:
    """Daemon thread: hold one LISTEN connection, relay notifications forever.

    Reconnects on any failure — a dropped DB connection must degrade to a few
    seconds of missed pushes (the 15s poll fallback covers the gap), never to
    a silently dead stream.
    """
    import select as select_mod

    import psycopg2
    import psycopg2.extensions

    dsn = db_engine.url.render_as_string(hide_password=False)
    # psycopg2 wants a plain postgresql:// scheme, not the SQLAlchemy driver tag.
    dsn = dsn.replace("postgresql+psycopg2://", "postgresql://")

    while True:
        try:
            conn = psycopg2.connect(dsn)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {CHANNEL};")
            log.info("so_events: LISTEN established")
            while True:
                ready = select_mod.select([conn], [], [], 25.0)
                if ready == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    note = conn.notifies.pop(0)
                    _dispatch_raw(note.payload)
        except Exception:  # noqa: BLE001
            log.warning("so_events listener died; reconnecting in 3s",
                        exc_info=True)
            time.sleep(3)


def _ensure_listener(loop: asyncio.AbstractEventLoop) -> None:
    """Start this process's LISTEN thread once (Postgres only)."""
    global _listener_started, _loop
    with _listener_lock:
        _loop = loop
        if _listener_started or not _is_postgres():
            return
        _listener_started = True
        threading.Thread(
            target=_listen_forever, name="so-events-listen", daemon=True
        ).start()


# ─── the SSE endpoint ───────────────────────────────────────────────────────


async def _event_stream(tenant_id: str) -> AsyncIterator[str]:
    """Yield SSE lines for one tenant. Heartbeats keep proxies from closing
    idle connections; the subscriber queue is cleaned up on disconnect."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.setdefault(tenant_id, []).append(q)
    try:
        # Immediate chunk so Railway's proxy flushes headers before the first
        # heartbeat — without it the client sits in 'reconnecting' for 20s.
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECS)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        subs = _subscribers.get(tenant_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _subscribers.pop(tenant_id, None)


@router.get("/v1/events")
async def sse_events(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    """Tenant-scoped SSE stream — every event type in the module docstring.

    Auth via Authorization: Bearer <session_token> header, or ?token= query
    param for EventSource callers that cannot set headers. 401 when invalid.
    """
    auth_header = authorization or (f"Bearer {token}" if token else None)
    tenant = tenant_from_session(auth_header)  # raises HTTPException(401)
    _ensure_listener(asyncio.get_running_loop())
    return StreamingResponse(
        _event_stream(tenant.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
