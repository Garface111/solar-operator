"""
Dev-only capture timeline endpoints. Gated by SO_DEV_ENABLED (mirrors the
pattern in api/dev_sandbox.py). Exposes per-tenant CaptureEvent rows for
debugging the ingest pipeline without shelling into prod.

Routes:
  GET /v1/dev/captures?limit=50&since=<iso>
    Recent captures for the calling tenant, grouped by capture_id,
    sorted newest-first. Each group includes all stage events.
  GET /v1/dev/captures/{capture_id}
    One capture's full event list + summary metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import select

from .account import tenant_from_session
from .db import SessionLocal
from .dev_sandbox import _require_dev
from .models import CaptureEvent

router = APIRouter(prefix="/v1/dev")


@router.get("/captures")
def list_captures(
    limit: int = Query(50, ge=1, le=200),
    since: Optional[str] = Query(None),
    authorization: Optional[str] = Header(default=None),
):
    """Recent captures grouped by capture_id, newest-first.

    Each item contains: capture_id, started_at, ended_at, stage_count,
    arrays_created, total_ms, has_error, client_hint, events[].
    """
    _require_dev()
    tenant = tenant_from_session(authorization)

    query = (
        select(CaptureEvent)
        .where(CaptureEvent.tenant_id == tenant.id)
        .order_by(CaptureEvent.created_at.desc())
    )
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)
            query = query.where(CaptureEvent.created_at >= since_dt)
        except ValueError:
            pass

    # Over-fetch to cover `limit` full captures (typical capture has ~5 events).
    with SessionLocal() as db:
        raw_events = db.execute(query.limit(limit * 20)).scalars().all()

    # Group by capture_id preserving newest-first order of first event seen.
    groups: dict[str, list] = {}
    group_order: list[str] = []
    for ev in raw_events:
        if ev.capture_id not in groups:
            groups[ev.capture_id] = []
            group_order.append(ev.capture_id)
        groups[ev.capture_id].append(ev)

    result = []
    for cid in group_order[:limit]:
        events = sorted(groups[cid], key=lambda e: e.created_at)
        first = events[0]
        last = events[-1]
        total_ms = sum(e.duration_ms or 0 for e in events)
        has_error = any(e.stage == "capture_error" for e in events)
        client_hint = next(
            (e.decision for e in events if e.stage.startswith("client_")),
            None,
        )
        arrays_created = sum(1 for e in events if e.stage == "array_created")
        result.append({
            "capture_id": cid,
            "started_at": first.created_at.isoformat(),
            "ended_at": last.created_at.isoformat(),
            "stage_count": len(events),
            "arrays_created": arrays_created,
            "total_ms": round(total_ms, 1),
            "has_error": has_error,
            "client_hint": client_hint,
            "events": [_ev_dict(e) for e in events],
        })

    return {"ok": True, "captures": result}


@router.get("/captures/{capture_id}")
def get_capture(
    capture_id: str,
    authorization: Optional[str] = Header(default=None),
):
    """One capture's full event list sorted chronologically."""
    _require_dev()
    tenant = tenant_from_session(authorization)

    with SessionLocal() as db:
        events = db.execute(
            select(CaptureEvent)
            .where(
                CaptureEvent.tenant_id == tenant.id,
                CaptureEvent.capture_id == capture_id,
            )
            .order_by(CaptureEvent.created_at)
        ).scalars().all()

    events_list = list(events)
    if not events_list:
        raise HTTPException(404, f"Capture {capture_id} not found")

    first = events_list[0]
    last = events_list[-1]
    total_ms = sum(e.duration_ms or 0 for e in events_list)
    has_error = any(e.stage == "capture_error" for e in events_list)
    client_hint = next(
        (e.decision for e in events_list if e.stage.startswith("client_")),
        None,
    )

    return {
        "ok": True,
        "capture_id": capture_id,
        "started_at": first.created_at.isoformat(),
        "ended_at": last.created_at.isoformat(),
        "total_ms": round(total_ms, 1),
        "has_error": has_error,
        "client_hint": client_hint,
        "events": [_ev_dict(e) for e in events_list],
    }


def _ev_dict(e: CaptureEvent) -> dict:
    return {
        "id": e.id,
        "stage": e.stage,
        "decision": e.decision,
        "payload_excerpt": e.payload_excerpt,
        "duration_ms": round(e.duration_ms, 2) if e.duration_ms is not None else None,
        "created_at": e.created_at.isoformat(),
    }
