"""Temporary + permanent email copy for offtaker invoices and generation reports.

Permanent templates live on Tenant (email_* / offtaker_email_*). This module
adds **scheduled / one-shot overrides** so Energy Agent (or the API) can:

  • rewrite subject/body for a window of time
  • inject an extra line for the next N sends only
  • schedule a change to start at a specific time and then expire

Channels:
  offtaker           → offtaker invoice letter (billing.delivery)
  generation_report  → client generation-report email (api.delivery)

Resolve order:
  1. Active override (starts_at ≤ now, not ended, not exhausted/cancelled)
  2. Permanent tenant template
  3. Built-in system default (caller supplies defaults)

After a successful real send, call ``record_send(override_id)`` so max_sends
countdowns expire the override automatically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from .db import SessionLocal
from .models import EmailCopyOverride, Tenant, now as model_now

log = logging.getLogger(__name__)

CHANNELS = frozenset({"offtaker", "generation_report"})
STATUSES_LIVE = frozenset({"scheduled", "active"})


def _utcnow() -> datetime:
    return datetime.utcnow()


def _parse_dt(val) -> Optional[datetime]:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    s = str(val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def permanent_templates(tenant: Tenant, channel: str) -> dict:
    """Return permanent subject/body/signoff stored on the tenant (may be None)."""
    if channel == "offtaker":
        return {
            "subject_template": getattr(tenant, "offtaker_email_subject_template", None),
            "body_template": getattr(tenant, "offtaker_email_body_template", None),
            "signoff": getattr(tenant, "email_signoff", None),
        }
    return {
        "subject_template": getattr(tenant, "email_subject_template", None),
        "body_template": getattr(tenant, "email_body_template", None),
        "signoff": getattr(tenant, "email_signoff", None),
    }


def set_permanent(
    db,
    tenant: Tenant,
    channel: str,
    *,
    subject_template: Optional[str] = None,
    body_template: Optional[str] = None,
    clear_subject: bool = False,
    clear_body: bool = False,
) -> dict:
    """Update permanent tenant templates. Pass clear_*=True to reset to system default."""
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {sorted(CHANNELS)}")
    if channel == "offtaker":
        if clear_subject:
            tenant.offtaker_email_subject_template = None
        elif subject_template is not None:
            tenant.offtaker_email_subject_template = subject_template.strip() or None
        if clear_body:
            tenant.offtaker_email_body_template = None
        elif body_template is not None:
            tenant.offtaker_email_body_template = body_template.strip() or None
    else:
        if clear_subject:
            tenant.email_subject_template = None
        elif subject_template is not None:
            tenant.email_subject_template = subject_template.strip() or None
        if clear_body:
            tenant.email_body_template = None
        elif body_template is not None:
            tenant.email_body_template = body_template.strip() or None
    db.flush()
    return permanent_templates(tenant, channel)


def _is_active(ov: EmailCopyOverride, now: datetime) -> bool:
    if ov.status not in STATUSES_LIVE and ov.status != "active":
        # scheduled becomes active when starts_at hits
        if ov.status != "scheduled":
            return False
    if ov.cancelled_at is not None:
        return False
    starts = ov.starts_at or datetime.min
    if starts > now:
        return False
    if ov.ends_at is not None and ov.ends_at <= now:
        return False
    if ov.max_sends is not None and (ov.sends_used or 0) >= ov.max_sends:
        return False
    return True


def list_overrides(db, tenant_id: str, channel: Optional[str] = None,
                   include_exhausted: bool = False) -> list[dict]:
    q = select(EmailCopyOverride).where(EmailCopyOverride.tenant_id == tenant_id)
    if channel:
        q = q.where(EmailCopyOverride.channel == channel)
    q = q.order_by(EmailCopyOverride.starts_at.desc(), EmailCopyOverride.id.desc())
    rows = db.execute(q).scalars().all()
    now = _utcnow()
    out = []
    for ov in rows:
        active = _is_active(ov, now)
        if not include_exhausted and ov.status in ("exhausted", "cancelled", "expired"):
            if not active:
                continue
        # promote scheduled → active when window opens (lazy)
        if ov.status == "scheduled" and active:
            ov.status = "active"
        d = _serialize(ov, active=active)
        out.append(d)
    return out


def _serialize(ov: EmailCopyOverride, *, active: bool) -> dict:
    return {
        "id": ov.id,
        "channel": ov.channel,
        "status": "active" if active and ov.status in STATUSES_LIVE | {"scheduled", "active"} else ov.status,
        "is_active_now": active,
        "subject_template": ov.subject_template,
        "body_template": ov.body_template,
        "body_append": ov.body_append,
        "starts_at": ov.starts_at.isoformat() + "Z" if ov.starts_at else None,
        "ends_at": ov.ends_at.isoformat() + "Z" if ov.ends_at else None,
        "max_sends": ov.max_sends,
        "sends_used": ov.sends_used or 0,
        "scope_kind": ov.scope_kind,
        "scope_id": ov.scope_id,
        "reason": ov.reason,
        "created_at": ov.created_at.isoformat() + "Z" if ov.created_at else None,
        "created_by": ov.created_by,
    }


def schedule_override(
    db,
    tenant_id: str,
    channel: str,
    *,
    subject_template: Optional[str] = None,
    body_template: Optional[str] = None,
    body_append: Optional[str] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    max_sends: Optional[int] = None,
    duration_hours: Optional[float] = None,
    scope_kind: Optional[str] = None,
    scope_id: Optional[str] = None,
    reason: Optional[str] = None,
    created_by: str = "energy_agent",
) -> EmailCopyOverride:
    """Create a temporary override. At least one of body_template / body_append /
    subject_template required. Expiry via ends_at, duration_hours, or max_sends."""
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {sorted(CHANNELS)}")
    if not any([(subject_template or "").strip(),
                (body_template or "").strip(),
                (body_append or "").strip()]):
        raise ValueError("Provide subject_template, body_template, and/or body_append")

    now = _utcnow()
    start = starts_at or now
    end = ends_at
    if end is None and duration_hours is not None and duration_hours > 0:
        end = start + timedelta(hours=float(duration_hours))
    if max_sends is not None and max_sends < 1:
        max_sends = 1
    if end is None and max_sends is None:
        # Default: one send cycle then auto-revert (the common "add a line this month" ask)
        max_sends = 1

    status = "active" if start <= now else "scheduled"
    ov = EmailCopyOverride(
        tenant_id=tenant_id,
        channel=channel,
        subject_template=(subject_template or "").strip() or None,
        body_template=(body_template or "").strip() or None,
        body_append=(body_append or "").strip() or None,
        starts_at=start,
        ends_at=end,
        max_sends=max_sends,
        sends_used=0,
        scope_kind=(scope_kind or "").strip() or None,
        scope_id=str(scope_id).strip() if scope_id is not None else None,
        status=status,
        reason=(reason or "").strip() or None,
        created_by=created_by or "energy_agent",
        created_at=now,
    )
    db.add(ov)
    db.flush()
    return ov


def cancel_override(db, tenant_id: str, override_id: int) -> Optional[dict]:
    ov = db.get(EmailCopyOverride, override_id)
    if ov is None or ov.tenant_id != tenant_id:
        return None
    ov.status = "cancelled"
    ov.cancelled_at = _utcnow()
    db.flush()
    return _serialize(ov, active=False)


def get_active_override(
    db,
    tenant_id: str,
    channel: str,
    *,
    scope_kind: Optional[str] = None,
    scope_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[EmailCopyOverride]:
    """Most recent active override for channel (+ optional scope). Prefer scoped."""
    now = now or _utcnow()
    rows = db.execute(
        select(EmailCopyOverride)
        .where(
            EmailCopyOverride.tenant_id == tenant_id,
            EmailCopyOverride.channel == channel,
            EmailCopyOverride.status.in_(("scheduled", "active")),
        )
        .order_by(EmailCopyOverride.id.desc())
    ).scalars().all()

    candidates = [ov for ov in rows if _is_active(ov, now)]
    if not candidates:
        return None

    # Prefer matching scope, then unscoped (applies to all)
    if scope_id is not None:
        scoped = [
            ov for ov in candidates
            if ov.scope_id and str(ov.scope_id) == str(scope_id)
            and (not scope_kind or ov.scope_kind == scope_kind)
        ]
        if scoped:
            return scoped[0]
    unscoped = [ov for ov in candidates if not ov.scope_id]
    return unscoped[0] if unscoped else None


def resolve_templates(
    db,
    tenant: Tenant,
    channel: str,
    *,
    scope_kind: Optional[str] = None,
    scope_id: Optional[str] = None,
) -> dict:
    """Merge permanent + active override into effective templates.

    Returns:
      subject_template, body_template, signoff,
      override_id (or None), body_append (or None), source ("override"|"permanent")
    """
    perm = permanent_templates(tenant, channel)
    ov = get_active_override(
        db, tenant.id, channel, scope_kind=scope_kind, scope_id=scope_id,
    )
    subject = perm["subject_template"]
    body = perm["body_template"]
    append = None
    oid = None
    source = "permanent"
    if ov is not None:
        oid = ov.id
        source = "override"
        if ov.subject_template:
            subject = ov.subject_template
        if ov.body_template:
            body = ov.body_template
        if ov.body_append:
            append = ov.body_append
        if ov.status == "scheduled":
            ov.status = "active"
    return {
        "subject_template": subject,
        "body_template": body,
        "signoff": perm["signoff"],
        "body_append": append,
        "override_id": oid,
        "source": source,
    }


def apply_body_append(body_template: Optional[str], append: Optional[str],
                      *, html: bool = True) -> Optional[str]:
    """Inject a temporary line/paragraph into the template before render."""
    if not append or not str(append).strip():
        return body_template
    a = str(append).strip()
    base = body_template or ""
    if html:
        # If template is HTML-ish, inject a paragraph before {{signoff}} if present
        block = f"<p><em>{_escape_html(a)}</em></p>"
        if "{{signoff}}" in base:
            return base.replace("{{signoff}}", block + "\n{{signoff}}", 1)
        return base + "\n" + block
    return (base + "\n\n" + a).strip()


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def record_send(db, override_id: Optional[int]) -> None:
    """Call after a successful non-test send so one-shot overrides expire."""
    if not override_id:
        return
    ov = db.get(EmailCopyOverride, override_id)
    if ov is None:
        return
    ov.sends_used = int(ov.sends_used or 0) + 1
    ov.last_send_at = _utcnow()
    if ov.max_sends is not None and ov.sends_used >= ov.max_sends:
        ov.status = "exhausted"
        log.info("email_copy_override %s exhausted after %s sends", ov.id, ov.sends_used)
    db.flush()


def expire_stale(db, tenant_id: Optional[str] = None) -> int:
    """Mark time-ended overrides as expired. Returns count updated."""
    now = _utcnow()
    q = select(EmailCopyOverride).where(
        EmailCopyOverride.status.in_(("scheduled", "active")),
        EmailCopyOverride.ends_at.is_not(None),
        EmailCopyOverride.ends_at <= now,
    )
    if tenant_id:
        q = q.where(EmailCopyOverride.tenant_id == tenant_id)
    n = 0
    for ov in db.execute(q).scalars().all():
        ov.status = "expired"
        n += 1
    if n:
        db.flush()
    return n
