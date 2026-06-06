"""
In-process SSE pub/sub for sandbox canvas live-push.

SINGLE-REPLICA CONSTRAINT: subscriber queues live entirely in-process using
asyncio.Queue. Events are NOT persisted and do NOT survive a process restart
or scale-out. Railway runs this app as a single replica today. Multi-replica
deployments would require Redis pub/sub or Postgres LISTEN/NOTIFY — not
implemented here. If Railway auto-scales, promote this constraint to a task.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from .account import tenant_from_session

log = logging.getLogger(__name__)
router = APIRouter()

# tenant_id → list of active SSE subscriber queues (one per open connection)
_subscribers: dict[str, list[asyncio.Queue]] = {}

HEARTBEAT_SECS = 20


def broadcast(tenant_id: str, event_type: str, payload: dict) -> None:
    """Broadcast an event to all SSE subscribers for this tenant.

    Must be called from within the asyncio event loop (i.e., from an async
    endpoint or coroutine, never from a background thread). Silently drops
    the event for slow consumers whose queue is full.
    """
    data = json.dumps({"type": event_type, **payload})
    for q in list(_subscribers.get(tenant_id, [])):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass  # slow consumer — drop


async def _subscribe(tenant_id: str) -> asyncio.Queue:
    """Register a new subscriber for tenant_id. Returns the subscriber queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.setdefault(tenant_id, []).append(q)
    return q


async def _unsubscribe(tenant_id: str, q: asyncio.Queue) -> None:
    """Remove a subscriber queue. Cleans up the tenant key when empty."""
    subs = _subscribers.get(tenant_id, [])
    try:
        subs.remove(q)
    except ValueError:
        pass
    if not subs:
        _subscribers.pop(tenant_id, None)


async def _event_stream(tenant_id: str) -> AsyncIterator[str]:
    """Async generator that yields SSE-formatted lines for the given tenant.

    Yields a heartbeat comment every HEARTBEAT_SECS seconds so proxies and
    load balancers do not close idle connections. Cleans up its subscriber
    queue when the generator is closed (client disconnect or server shutdown).
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.setdefault(tenant_id, []).append(q)
    try:
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
    """Tenant-scoped SSE stream for live canvas push.

    Streams capture.landed events when the extension lands a new capture.
    Auth via Authorization: Bearer <session_token> header, or ?token= query
    param as fallback for EventSource callers that cannot set headers.
    Returns 401 for unauthenticated requests — never 500.
    """
    auth_header = authorization or (f"Bearer {token}" if token else None)
    tenant = tenant_from_session(auth_header)  # raises HTTPException(401) if invalid
    return StreamingResponse(
        _event_stream(tenant.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx/Railway proxy buffering
            "Connection": "keep-alive",
        },
    )
