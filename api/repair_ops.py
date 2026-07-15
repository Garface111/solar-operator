"""Ops / repair healing for Array Operator — service contacts + repair tickets.

Warranty claims (`warranty_claims.py`) email the *manufacturer*. This module is
the human ops network: who installs/repairs the arrays, which site they cover,
and check-ins when something is down.

Promise: Energy Agent knows the ops team, can contact them, and follows up on
repair status without the owner babysitting every ticket.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from . import notify
from .db import SessionLocal
from .models import (
    Array,
    ArrayServiceAssignment,
    Inverter,
    RepairCheckIn,
    RepairTicket,
    ServiceContact,
    Tenant,
    now,
)

log = logging.getLogger(__name__)
router = APIRouter()

# Hardware states that auto-open a repair ticket (same warrantable set + comm_gap
# as a softer ops signal when a contact is assigned).
AUTO_OPEN_FAILS = ("dead", "fault")
SOFT_OPEN_FAILS = ("comm_gap",)  # only if contact assigned and auto_open on

ACTIVE_TICKET_STATUSES = (
    "open", "waiting_reply", "scheduled", "in_progress",
)
TERMINAL_STATUSES = ("resolved", "cancelled", "cleared")
VALID_CHECKIN_MODES = ("off", "manual", "auto", "delay")
VALID_ROLES = (
    "installer", "om", "electrician", "technician",
    "general_contractor", "other",
)
VALID_TICKET_STATUSES = ACTIVE_TICKET_STATUSES + TERMINAL_STATUSES


# ── helpers ───────────────────────────────────────────────────────────────────

def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _email_ok(addr: str | None) -> bool:
    if not addr or not isinstance(addr, str):
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr.strip()))


def serialize_contact(c: ServiceContact) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "company": c.company,
        "role": c.role,
        "email": c.email,
        "phone": c.phone,
        "notes": c.notes,
        "is_default": bool(c.is_default),
        "active": bool(c.active),
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


def serialize_ticket(t: RepairTicket, *, contact: ServiceContact | None = None) -> dict:
    return {
        "id": t.id,
        "array_id": t.array_id,
        "inverter_id": t.inverter_id,
        "contact_id": t.contact_id,
        "warranty_claim_id": t.warranty_claim_id,
        "site_name": t.site_name,
        "inv_name": t.inv_name,
        "serial": t.serial,
        "vendor": t.vendor,
        "fail_type": t.fail_type,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "severity": t.severity,
        "source": t.source,
        "evidence": t.evidence or {},
        "draft_checkin": t.draft_checkin or {},
        "next_checkin_at": _iso(t.next_checkin_at),
        "last_checkin_at": _iso(t.last_checkin_at),
        "checkin_count": t.checkin_count or 0,
        "scheduled_for": _iso(t.scheduled_for),
        "tech_note": t.tech_note,
        "opened_at": _iso(t.opened_at),
        "resolved_at": _iso(t.resolved_at),
        "cancelled_at": _iso(t.cancelled_at),
        "cleared_at": _iso(t.cleared_at),
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
        "contact": serialize_contact(contact) if contact else None,
    }


def serialize_checkin(c: RepairCheckIn) -> dict:
    return {
        "id": c.id,
        "ticket_id": c.ticket_id,
        "contact_id": c.contact_id,
        "channel": c.channel,
        "direction": c.direction,
        "subject": c.subject,
        "body": c.body,
        "sent_to": c.sent_to,
        "sent_ok": bool(c.sent_ok),
        "via": c.via,
        "created_at": _iso(c.created_at),
    }


def effective_checkin_mode(tenant: Tenant) -> str:
    mode = (tenant.repair_checkin_mode or "manual").strip().lower()
    return mode if mode in VALID_CHECKIN_MODES else "manual"


def checkin_interval_hours(tenant: Tenant) -> int:
    try:
        h = int(tenant.repair_checkin_hours or 48)
    except (TypeError, ValueError):
        h = 48
    return max(6, min(168 * 2, h))  # 6h .. 14d


# ── contacts ──────────────────────────────────────────────────────────────────

def list_contacts(db, tenant_id: str, *, include_inactive: bool = False) -> list[ServiceContact]:
    q = select(ServiceContact).where(
        ServiceContact.tenant_id == tenant_id,
        ServiceContact.deleted_at.is_(None),
    )
    if not include_inactive:
        q = q.where(ServiceContact.active.is_(True))
    q = q.order_by(ServiceContact.is_default.desc(), ServiceContact.name.asc())
    return list(db.execute(q).scalars().all())


def get_contact(db, tenant_id: str, contact_id: int) -> ServiceContact | None:
    c = db.get(ServiceContact, contact_id)
    if c is None or c.tenant_id != tenant_id or c.deleted_at is not None:
        return None
    return c


def upsert_contact(
    db,
    tenant_id: str,
    *,
    contact_id: int | None = None,
    name: str,
    company: str | None = None,
    role: str = "om",
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    is_default: bool = False,
    active: bool = True,
) -> ServiceContact:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    role = (role or "om").strip().lower()
    if role not in VALID_ROLES:
        role = "other"
    email = (email or "").strip() or None
    if email and not _email_ok(email):
        raise ValueError(f"invalid email: {email}")
    phone = (phone or "").strip() or None
    company = (company or "").strip() or None
    notes = (notes or "").strip() or None

    if contact_id:
        c = get_contact(db, tenant_id, contact_id)
        if c is None:
            raise ValueError("contact not found")
    else:
        c = ServiceContact(tenant_id=tenant_id, name=name)
        db.add(c)

    c.name = name
    c.company = company
    c.role = role
    c.email = email
    c.phone = phone
    c.notes = notes
    c.active = bool(active)
    c.is_default = bool(is_default)
    db.flush()

    if c.is_default:
        # Only one default per tenant
        for other in list_contacts(db, tenant_id, include_inactive=True):
            if other.id != c.id and other.is_default:
                other.is_default = False
        db.flush()
    return c


def soft_delete_contact(db, tenant_id: str, contact_id: int) -> None:
    c = get_contact(db, tenant_id, contact_id)
    if c is None:
        raise ValueError("contact not found")
    c.active = False
    c.deleted_at = now()
    c.is_default = False
    db.flush()


def assign_array_contact(
    db,
    tenant_id: str,
    array_id: int,
    contact_id: int,
    *,
    kind: str = "primary",
) -> ArrayServiceAssignment:
    arr = db.get(Array, array_id)
    if arr is None or arr.tenant_id != tenant_id or getattr(arr, "deleted_at", None):
        raise ValueError("array not found")
    c = get_contact(db, tenant_id, contact_id)
    if c is None or not c.active:
        raise ValueError("contact not found or inactive")

    kind = (kind or "primary").strip().lower()
    if kind not in ("primary", "backup", "monitoring_only"):
        kind = "primary"

    if kind == "primary":
        # demote other primaries on this array
        existing = db.execute(
            select(ArrayServiceAssignment).where(
                ArrayServiceAssignment.tenant_id == tenant_id,
                ArrayServiceAssignment.array_id == array_id,
                ArrayServiceAssignment.kind == "primary",
            )
        ).scalars().all()
        for row in existing:
            if row.contact_id != contact_id:
                row.kind = "backup"

    row = db.execute(
        select(ArrayServiceAssignment).where(
            ArrayServiceAssignment.array_id == array_id,
            ArrayServiceAssignment.contact_id == contact_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = ArrayServiceAssignment(
            tenant_id=tenant_id,
            array_id=array_id,
            contact_id=contact_id,
            kind=kind,
        )
        db.add(row)
    else:
        row.kind = kind
    db.flush()
    return row


def unassign_array_contact(db, tenant_id: str, array_id: int, contact_id: int) -> None:
    row = db.execute(
        select(ArrayServiceAssignment).where(
            ArrayServiceAssignment.tenant_id == tenant_id,
            ArrayServiceAssignment.array_id == array_id,
            ArrayServiceAssignment.contact_id == contact_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("assignment not found")
    db.delete(row)
    db.flush()


def resolve_contact_for_array(db, tenant_id: str, array_id: int | None) -> ServiceContact | None:
    """Primary assignment → any assignment → tenant default contact."""
    if array_id:
        rows = db.execute(
            select(ArrayServiceAssignment)
            .where(
                ArrayServiceAssignment.tenant_id == tenant_id,
                ArrayServiceAssignment.array_id == array_id,
            )
            .order_by(
                # primary first
                ArrayServiceAssignment.kind.asc(),
                ArrayServiceAssignment.id.asc(),
            )
        ).scalars().all()
        # Prefer kind == primary
        ordered = sorted(rows, key=lambda r: (0 if r.kind == "primary" else 1, r.id))
        for r in ordered:
            c = get_contact(db, tenant_id, r.contact_id)
            if c and c.active:
                return c
    # default
    defaults = db.execute(
        select(ServiceContact).where(
            ServiceContact.tenant_id == tenant_id,
            ServiceContact.deleted_at.is_(None),
            ServiceContact.active.is_(True),
            ServiceContact.is_default.is_(True),
        )
    ).scalars().all()
    return defaults[0] if defaults else None


def assignments_for_tenant(db, tenant_id: str) -> list[dict]:
    rows = db.execute(
        select(ArrayServiceAssignment).where(ArrayServiceAssignment.tenant_id == tenant_id)
    ).scalars().all()
    out = []
    for r in rows:
        arr = db.get(Array, r.array_id)
        c = get_contact(db, tenant_id, r.contact_id)
        out.append({
            "array_id": r.array_id,
            "array_name": getattr(arr, "name", None) if arr else None,
            "contact_id": r.contact_id,
            "contact_name": c.name if c else None,
            "kind": r.kind,
        })
    return out


# ── draft / send check-in ─────────────────────────────────────────────────────

def build_checkin_draft(
    ticket: RepairTicket,
    tenant: Tenant,
    contact: ServiceContact | None,
    *,
    sequence: int | None = None,
) -> dict:
    """Plain-English status check-in email for the assigned tech."""
    owner = (
        getattr(tenant, "company_name", None)
        or getattr(tenant, "operator_name", None)
        or getattr(tenant, "name", None)
        or "the array owner"
    )
    owner_email = getattr(tenant, "contact_email", None) or ""
    site = ticket.site_name or (f"Array #{ticket.array_id}" if ticket.array_id else "a site")
    inv = ticket.inv_name or ticket.serial or "inverter"
    fail = ticket.fail_type or "issue"
    n = sequence if sequence is not None else ((ticket.checkin_count or 0) + 1)

    if n <= 1:
        subject = f"Repair needed — {site} ({fail})"
        opener = (
            f"Hi {contact.name.split()[0] if contact and contact.name else 'there'},\n\n"
            f"This is {owner}'s monitoring agent. We detected a {fail} on "
            f"{site} ({inv}"
            f"{', serial ' + ticket.serial if ticket.serial else ''}"
            f"{', ' + ticket.vendor if ticket.vendor else ''}).\n\n"
        )
    else:
        subject = f"Status check-in #{n} — {site} repair"
        opener = (
            f"Hi {contact.name.split()[0] if contact and contact.name else 'there'},\n\n"
            f"Quick check-in from {owner}'s monitoring agent on the open repair at "
            f"{site} ({inv}). This is follow-up #{n}.\n\n"
        )

    evidence = ticket.evidence or {}
    evid_lines = []
    if evidence.get("diagnosis"):
        evid_lines.append(f"Diagnosis: {evidence['diagnosis']}")
    if evidence.get("status"):
        evid_lines.append(f"Inverter status: {evidence['status']}")
    if evidence.get("lost_kwh_window") is not None:
        evid_lines.append(f"Lost energy (window): ~{evidence['lost_kwh_window']} kWh")
    if evidence.get("lost_usd_yr") is not None:
        evid_lines.append(f"At-stake (annualized): ~${evidence['lost_usd_yr']}")
    if ticket.description:
        evid_lines.append(f"Notes: {ticket.description}")

    body = (
        opener
        + ("What we're seeing:\n- " + "\n- ".join(evid_lines) + "\n\n" if evid_lines else "")
        + "Could you reply with a short status?\n"
        + "  1) Acknowledged / scheduled visit date\n"
        + "  2) Parts on order\n"
        + "  3) Repair completed\n"
        + "  4) Needs owner action (access, warranty RMA, etc.)\n\n"
        + (f"Reply-to / owner contact: {owner_email}\n" if owner_email else "")
        + f"Ticket #{ticket.id} · site: {site}\n"
        + "— Array Operator Energy Agent\n"
    )
    to = (contact.email if contact and contact.email else None) or owner_email
    return {"to": to, "subject": subject, "body": body}


def _schedule_next_checkin(tenant: Tenant, ticket: RepairTicket, *, first: bool = False) -> None:
    mode = effective_checkin_mode(tenant)
    hours = checkin_interval_hours(tenant)
    if mode == "off":
        ticket.next_checkin_at = None
        return
    if mode == "manual":
        # Ready for agent/owner to send; no auto fire
        ticket.next_checkin_at = None
        return
    if mode == "delay" and first:
        ticket.next_checkin_at = now() + timedelta(hours=hours)
        return
    if mode == "auto":
        ticket.next_checkin_at = now() + timedelta(hours=hours if not first else 0)
        return
    if mode == "delay":
        ticket.next_checkin_at = now() + timedelta(hours=hours)


def send_checkin(
    db,
    tenant: Tenant,
    ticket: RepairTicket,
    *,
    via: str = "agent",
    to_override: str | None = None,
    subject_override: str | None = None,
    body_override: str | None = None,
) -> RepairCheckIn:
    """Send the drafted check-in email and log a RepairCheckIn row."""
    contact = get_contact(db, tenant.id, ticket.contact_id) if ticket.contact_id else None
    draft = ticket.draft_checkin or build_checkin_draft(ticket, tenant, contact)
    if subject_override:
        draft["subject"] = subject_override
    if body_override:
        draft["body"] = body_override
    if to_override:
        draft["to"] = to_override

    to = (draft.get("to") or "").strip()
    subject = (draft.get("subject") or f"Repair check-in — ticket #{ticket.id}").strip()
    body = (draft.get("body") or "").strip()
    if not body:
        raise ValueError("check-in body is empty")

    # Prefer tech email; fall back to owner as forward packet (safe default)
    owner_email = getattr(tenant, "contact_email", None)
    go_to = to if _email_ok(to) else (owner_email or "")
    if not _email_ok(go_to):
        raise ValueError("no valid recipient email (contact or owner)")

    # If we only have owner email and draft.to was tech-less, wrap as forward packet
    send_body = body
    if owner_email and go_to.lower() == owner_email.lower() and contact and contact.email:
        send_body = (
            f"Forward-ready check-in for {contact.name}"
            f"{' at ' + contact.company if contact.company else ''}"
            f" <{contact.email}>:\n\n---\n\n{body}"
        )
        subject = f"[Forward to tech] {subject}"

    ok = notify.send_repair_checkin_email(
        to=go_to,
        subject=subject,
        body_text=send_body,
        reply_to=owner_email,
        from_name=(getattr(tenant, "company_name", None) or tenant.name or "Array Operator"),
    )

    row = RepairCheckIn(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        contact_id=ticket.contact_id,
        channel="email",
        direction="outbound",
        subject=subject,
        body=send_body,
        sent_to=go_to,
        sent_ok=bool(ok),
        via=via,
    )
    db.add(row)

    if ok:
        ticket.last_checkin_at = now()
        ticket.checkin_count = (ticket.checkin_count or 0) + 1
        if ticket.status == "open":
            ticket.status = "waiting_reply"
        ticket.draft_checkin = build_checkin_draft(
            ticket, tenant, contact, sequence=(ticket.checkin_count or 0) + 1,
        )
        _schedule_next_checkin(tenant, ticket, first=False)
    db.flush()
    if not ok:
        log.warning("repair check-in send failed ticket=%s tenant=%s", ticket.id, tenant.id)
        raise RuntimeError("email send failed")
    return row


# ── tickets ───────────────────────────────────────────────────────────────────

def _active_ticket_for_inverter(db, tenant_id: str, inverter_id: int | None, array_id: int | None):
    q = select(RepairTicket).where(
        RepairTicket.tenant_id == tenant_id,
        RepairTicket.status.in_(ACTIVE_TICKET_STATUSES),
    )
    if inverter_id:
        q = q.where(RepairTicket.inverter_id == inverter_id)
    elif array_id:
        q = q.where(RepairTicket.array_id == array_id, RepairTicket.inverter_id.is_(None))
    else:
        return None
    return db.execute(q.order_by(RepairTicket.id.desc())).scalars().first()


def open_ticket(
    db,
    tenant: Tenant,
    *,
    array_id: int | None = None,
    inverter_id: int | None = None,
    contact_id: int | None = None,
    fail_type: str = "other",
    title: str | None = None,
    description: str | None = None,
    severity: str = "critical",
    source: str = "manual",
    evidence: dict | None = None,
    site_name: str | None = None,
    inv_name: str | None = None,
    serial: str | None = None,
    vendor: str | None = None,
    warranty_claim_id: int | None = None,
) -> RepairTicket:
    # Resolve snapshots from DB when possible
    arr = db.get(Array, array_id) if array_id else None
    if arr and arr.tenant_id != tenant.id:
        raise ValueError("array not found")
    inv = db.get(Inverter, inverter_id) if inverter_id else None
    if inv and inv.tenant_id != tenant.id:
        raise ValueError("inverter not found")

    if inv and not array_id:
        array_id = inv.array_id
        arr = db.get(Array, array_id) if array_id else arr

    site_name = site_name or (arr.name if arr else None)
    inv_name = inv_name or (getattr(inv, "name", None) if inv else None)
    serial = serial or (getattr(inv, "serial", None) if inv else None)
    vendor = vendor or (getattr(inv, "vendor", None) if inv else None)

    # Reuse active ticket for same unit
    existing = _active_ticket_for_inverter(db, tenant.id, inverter_id, array_id)
    if existing:
        return existing

    contact = None
    if contact_id:
        contact = get_contact(db, tenant.id, contact_id)
    if contact is None:
        contact = resolve_contact_for_array(db, tenant.id, array_id)

    fail_type = (fail_type or "other").strip().lower()
    severity = severity if severity in ("critical", "warning", "info") else "critical"
    title = (title or "").strip() or (
        f"{fail_type.title()} — {site_name or 'site'}"
        + (f" / {inv_name or serial}" if (inv_name or serial) else "")
    )

    ticket = RepairTicket(
        tenant_id=tenant.id,
        array_id=array_id,
        inverter_id=inverter_id,
        contact_id=contact.id if contact else None,
        warranty_claim_id=warranty_claim_id,
        site_name=site_name,
        inv_name=inv_name,
        serial=serial,
        vendor=vendor,
        fail_type=fail_type,
        title=title[:200],
        description=(description or None),
        status="open",
        severity=severity,
        source=source,
        evidence=evidence or {},
    )
    db.add(ticket)
    db.flush()
    ticket.draft_checkin = build_checkin_draft(ticket, tenant, contact)
    _schedule_next_checkin(tenant, ticket, first=True)
    db.flush()
    return ticket


def update_ticket(
    db,
    tenant: Tenant,
    ticket: RepairTicket,
    *,
    status: str | None = None,
    contact_id: int | None = None,
    tech_note: str | None = None,
    scheduled_for=None,
    description: str | None = None,
    clear_scheduled: bool = False,
) -> RepairTicket:
    if status is not None:
        status = status.strip().lower()
        if status not in VALID_TICKET_STATUSES:
            raise ValueError(f"invalid status: {status}")
        ticket.status = status
        if status == "resolved":
            ticket.resolved_at = now()
            ticket.next_checkin_at = None
        elif status == "cancelled":
            ticket.cancelled_at = now()
            ticket.next_checkin_at = None
        elif status == "cleared":
            ticket.cleared_at = now()
            ticket.next_checkin_at = None
        elif status in ACTIVE_TICKET_STATUSES and ticket.status in TERMINAL_STATUSES:
            # reopen path handled by caller usually
            pass

    if contact_id is not None:
        if contact_id == 0:
            ticket.contact_id = None
        else:
            c = get_contact(db, tenant.id, contact_id)
            if c is None:
                raise ValueError("contact not found")
            ticket.contact_id = c.id
            ticket.draft_checkin = build_checkin_draft(ticket, tenant, c)

    if tech_note is not None:
        ticket.tech_note = tech_note.strip() or None
    if description is not None:
        ticket.description = description.strip() or None
    if clear_scheduled:
        ticket.scheduled_for = None
    elif scheduled_for is not None:
        ticket.scheduled_for = scheduled_for
        if ticket.status in ("open", "waiting_reply"):
            ticket.status = "scheduled"

    db.flush()
    return ticket


def list_tickets(
    db,
    tenant_id: str,
    *,
    status: str | None = None,
    array_id: int | None = None,
    active_only: bool = False,
    limit: int = 100,
) -> list[RepairTicket]:
    q = select(RepairTicket).where(RepairTicket.tenant_id == tenant_id)
    if active_only:
        q = q.where(RepairTicket.status.in_(ACTIVE_TICKET_STATUSES))
    if status:
        q = q.where(RepairTicket.status == status)
    if array_id:
        q = q.where(RepairTicket.array_id == array_id)
    q = q.order_by(RepairTicket.opened_at.desc()).limit(max(1, min(300, limit)))
    return list(db.execute(q).scalars().all())


def log_inbound_note(
    db,
    tenant: Tenant,
    ticket: RepairTicket,
    note: str,
    *,
    via: str = "agent",
) -> RepairCheckIn:
    note = (note or "").strip()
    if not note:
        raise ValueError("note is required")
    row = RepairCheckIn(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        contact_id=ticket.contact_id,
        channel="internal",
        direction="note",
        subject="Status note",
        body=note,
        sent_to=None,
        sent_ok=True,
        via=via,
    )
    db.add(row)
    ticket.tech_note = note
    # Heuristic status bumps from note keywords
    low = note.lower()
    if any(w in low for w in ("fixed", "replaced", "repaired", "back online", "resolved")):
        if ticket.status in ACTIVE_TICKET_STATUSES:
            ticket.status = "resolved"
            ticket.resolved_at = now()
            ticket.next_checkin_at = None
    elif any(w in low for w in ("scheduled", "will visit", "on site", "appointment")):
        if ticket.status in ("open", "waiting_reply"):
            ticket.status = "scheduled"
    elif any(w in low for w in ("working on", "in progress", "parts ordered")):
        if ticket.status in ("open", "waiting_reply", "scheduled"):
            ticket.status = "in_progress"
    db.flush()
    return row


# ── reconcile against fleet ───────────────────────────────────────────────────

def reconcile(db, tenant: Tenant, tree: Optional[dict] = None) -> dict:
    """Open tickets for new dead/fault units; clear tickets when hardware recovers.

    Requires repair_auto_open (default True) and a resolvable service contact
    (assignment or tenant default) — never emails on its own unless mode=auto
    and process_due runs.
    """
    opened = closed = 0
    if not getattr(tenant, "repair_auto_open", True):
        return {"opened": 0, "closed": 0, "skipped": "auto_open_off"}

    if tree is None:
        try:
            from . import inverter_fleet
            tree = inverter_fleet.build_fleet_tree(
                db, tenant, force_refresh=False, stable_verdicts=True,
            )
        except Exception as exc:
            log.warning("repair reconcile fleet tree failed for %s: %s", tenant.id, exc)
            return {"opened": 0, "closed": 0, "error": str(exc)}

    columns = list((tree or {}).get("columns") or [])
    # Map inverter_id → current status + context
    live: dict[int, dict] = {}
    for col in columns:
        array_id = col.get("array_id")
        site = col.get("array_name")
        for inv in col.get("inverters") or []:
            iid = inv.get("inverter_id")
            if not iid:
                continue
            live[int(iid)] = {
                "inv": inv,
                "array_id": array_id,
                "site_name": site,
                "status": (inv.get("status") or "ok").lower(),
            }

    # Close recovered
    active = list_tickets(db, tenant.id, active_only=True, limit=300)
    for ticket in active:
        if not ticket.inverter_id:
            continue
        info = live.get(int(ticket.inverter_id))
        if info is None:
            continue
        st = info["status"]
        if st in ("ok",) or st not in AUTO_OPEN_FAILS + SOFT_OPEN_FAILS:
            # Recovered enough to clear if never contacted, else resolve
            if (ticket.checkin_count or 0) == 0 and ticket.status == "open":
                ticket.status = "cleared"
                ticket.cleared_at = now()
                ticket.next_checkin_at = None
                closed += 1
            elif st == "ok" and ticket.status in ACTIVE_TICKET_STATUSES:
                ticket.status = "resolved"
                ticket.resolved_at = now()
                ticket.next_checkin_at = None
                ticket.tech_note = (ticket.tech_note or "") + (
                    "\n[auto] inverter returned to ok" if ticket.tech_note else "[auto] inverter returned to ok"
                )
                closed += 1

    # Open new
    for iid, info in live.items():
        st = info["status"]
        if st not in AUTO_OPEN_FAILS:
            continue
        inv = info["inv"]
        array_id = info["array_id"]
        contact = resolve_contact_for_array(db, tenant.id, array_id)
        if contact is None:
            continue  # no ops person known — don't open an orphan ticket
        existing = _active_ticket_for_inverter(db, tenant.id, iid, array_id)
        if existing:
            continue
        evidence = {
            "status": st,
            "diagnosis": inv.get("diagnosis") or inv.get("why"),
            "peer_index": inv.get("peer_index"),
            "window_kwh": inv.get("window_kwh"),
            "nameplate_kw": inv.get("nameplate_kw"),
        }
        open_ticket(
            db,
            tenant,
            array_id=array_id,
            inverter_id=iid,
            contact_id=contact.id,
            fail_type=st,
            severity="critical" if st in ("dead", "fault") else "warning",
            source="auto",
            evidence=evidence,
            site_name=info.get("site_name"),
            inv_name=inv.get("name"),
            serial=inv.get("sn") or inv.get("serial"),
            vendor=inv.get("vendor"),
        )
        opened += 1

    db.commit()
    return {"opened": opened, "closed": closed}


def process_due(db, tenant: Tenant) -> int:
    """Fire auto check-ins whose next_checkin_at is due (mode auto|delay only)."""
    mode = effective_checkin_mode(tenant)
    if mode not in ("auto", "delay"):
        return 0
    due = db.execute(
        select(RepairTicket).where(
            RepairTicket.tenant_id == tenant.id,
            RepairTicket.status.in_(ACTIVE_TICKET_STATUSES),
            RepairTicket.next_checkin_at.isnot(None),
            RepairTicket.next_checkin_at <= now(),
        )
    ).scalars().all()
    sent = 0
    for ticket in due:
        try:
            send_checkin(db, tenant, ticket, via="auto")
            sent += 1
        except Exception as exc:
            log.warning(
                "auto repair check-in failed ticket=%s: %s", ticket.id, exc,
            )
            # Push next attempt out so we don't tight-loop
            ticket.next_checkin_at = now() + timedelta(hours=checkin_interval_hours(tenant))
    if sent:
        db.commit()
    else:
        db.commit()
    return sent


def summarize_tickets(tickets: list[RepairTicket]) -> dict:
    active = [t for t in tickets if t.status in ACTIVE_TICKET_STATUSES]
    return {
        "open": len(active),
        "by_status": {
            s: sum(1 for t in tickets if t.status == s)
            for s in VALID_TICKET_STATUSES
            if any(t.status == s for t in tickets)
        },
        "awaiting_reply": sum(1 for t in tickets if t.status == "waiting_reply"),
        "scheduled": sum(1 for t in tickets if t.status == "scheduled"),
        "overdue_checkin": sum(
            1 for t in active
            if t.next_checkin_at and t.next_checkin_at <= now()
        ),
    }


def ops_overview(db, tenant: Tenant, tree: Optional[dict] = None) -> dict:
    """One-shot: contacts, assignments, active tickets, down arrays needing help."""
    contacts = list_contacts(db, tenant.id)
    tickets = list_tickets(db, tenant.id, limit=100)
    active = [t for t in tickets if t.status in ACTIVE_TICKET_STATUSES]
    assigns = assignments_for_tenant(db, tenant.id)

    needs: list[dict] = []
    if tree is None:
        try:
            from . import inverter_fleet
            tree = inverter_fleet.build_fleet_tree(
                db, tenant, force_refresh=False, stable_verdicts=True,
            )
        except Exception as exc:
            tree = {"error": str(exc), "columns": []}

    for col in (tree or {}).get("columns") or []:
        bad = [
            inv for inv in (col.get("inverters") or [])
            if (inv.get("status") or "ok") in AUTO_OPEN_FAILS + ("underperforming", "comm_gap")
        ]
        if not bad and (col.get("alert") or {}).get("status") not in (
            "fault", "error", "dead", "critical",
        ):
            continue
        aid = col.get("array_id")
        contact = resolve_contact_for_array(db, tenant.id, aid)
        open_here = [t for t in active if t.array_id == aid]
        needs.append({
            "array_id": aid,
            "array_name": col.get("array_name"),
            "alert": col.get("alert"),
            "problem_inverters": [
                {
                    "inverter_id": i.get("inverter_id"),
                    "name": i.get("name"),
                    "sn": i.get("sn"),
                    "status": i.get("status"),
                    "diagnosis": i.get("diagnosis"),
                }
                for i in bad[:12]
            ],
            "contact": serialize_contact(contact) if contact else None,
            "open_tickets": [serialize_ticket(t) for t in open_here],
            "next_step": (
                "Send check-in to assigned tech"
                if contact and open_here
                else (
                    "Open repair ticket + assign tech"
                    if contact
                    else "Add a service contact (installer / O&M) then open a ticket"
                )
            ),
        })

    contact_by_id = {c.id: c for c in contacts}
    # also load contacts referenced by tickets that might be inactive
    for t in active:
        if t.contact_id and t.contact_id not in contact_by_id:
            c = get_contact(db, tenant.id, t.contact_id)
            if c:
                contact_by_id[c.id] = c

    return {
        "settings": {
            "checkin_mode": effective_checkin_mode(tenant),
            "checkin_hours": checkin_interval_hours(tenant),
            "auto_open": bool(getattr(tenant, "repair_auto_open", True)),
        },
        "contacts": [serialize_contact(c) for c in contacts],
        "assignments": assigns,
        "tickets": [
            serialize_ticket(t, contact=contact_by_id.get(t.contact_id) if t.contact_id else None)
            for t in tickets if t.status != "cleared"
        ],
        "summary": summarize_tickets(tickets),
        "sites_needing_repair": needs,
    }


# ── REST ──────────────────────────────────────────────────────────────────────

def _tenant(authorization: str | None) -> Tenant:
    from .array_owners import _tenant_from_bearer
    return _tenant_from_bearer(authorization)


def _get_ticket(db, tenant: Tenant, ticket_id: int) -> RepairTicket:
    t = db.get(RepairTicket, ticket_id)
    if t is None or t.tenant_id != tenant.id:
        raise HTTPException(404, "Ticket not found")
    return t


class ContactBody(BaseModel):
    name: str
    company: Optional[str] = None
    role: Optional[str] = "om"
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    is_default: Optional[bool] = False
    active: Optional[bool] = True


class AssignBody(BaseModel):
    array_id: int
    contact_id: int
    kind: Optional[str] = "primary"


class TicketCreateBody(BaseModel):
    array_id: Optional[int] = None
    inverter_id: Optional[int] = None
    contact_id: Optional[int] = None
    fail_type: Optional[str] = "other"
    title: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = "critical"


class TicketPatchBody(BaseModel):
    status: Optional[str] = None
    contact_id: Optional[int] = None
    tech_note: Optional[str] = None
    description: Optional[str] = None
    scheduled_for: Optional[str] = None  # ISO


class CheckInSendBody(BaseModel):
    to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


class NoteBody(BaseModel):
    note: str


class SettingsBody(BaseModel):
    checkin_mode: Optional[str] = None
    checkin_hours: Optional[int] = None
    auto_open: Optional[bool] = None


@router.get("/v1/array-owners/ops")
def ops_overview_ep(
    reconcile_first: int = 1,
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if reconcile_first:
            try:
                reconcile(db, t)
                process_due(db, t)
            except Exception as exc:
                log.warning("ops reconcile failed for %s: %s", t.id, exc)
        return {"ok": True, **ops_overview(db, t)}


@router.get("/v1/array-owners/ops/contacts")
def list_contacts_ep(authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        contacts = list_contacts(db, tenant.id)
        assigns = assignments_for_tenant(db, tenant.id)
        return {
            "ok": True,
            "contacts": [serialize_contact(c) for c in contacts],
            "assignments": assigns,
        }


@router.post("/v1/array-owners/ops/contacts")
def create_contact_ep(body: ContactBody, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        try:
            c = upsert_contact(
                db, tenant.id,
                name=body.name, company=body.company, role=body.role or "om",
                email=body.email, phone=body.phone, notes=body.notes,
                is_default=bool(body.is_default), active=bool(body.active if body.active is not None else True),
            )
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "contact": serialize_contact(c)}


@router.patch("/v1/array-owners/ops/contacts/{contact_id}")
def patch_contact_ep(
    contact_id: int,
    body: ContactBody,
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        try:
            c = upsert_contact(
                db, tenant.id, contact_id=contact_id,
                name=body.name, company=body.company, role=body.role or "om",
                email=body.email, phone=body.phone, notes=body.notes,
                is_default=bool(body.is_default),
                active=bool(body.active if body.active is not None else True),
            )
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "contact": serialize_contact(c)}


@router.delete("/v1/array-owners/ops/contacts/{contact_id}")
def delete_contact_ep(contact_id: int, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        try:
            soft_delete_contact(db, tenant.id, contact_id)
            db.commit()
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {"ok": True}


@router.post("/v1/array-owners/ops/assign")
def assign_ep(body: AssignBody, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        try:
            row = assign_array_contact(
                db, tenant.id, body.array_id, body.contact_id, kind=body.kind or "primary",
            )
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {
            "ok": True,
            "assignment": {
                "array_id": row.array_id,
                "contact_id": row.contact_id,
                "kind": row.kind,
            },
        }


@router.post("/v1/array-owners/ops/unassign")
def unassign_ep(body: AssignBody, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        try:
            unassign_array_contact(db, tenant.id, body.array_id, body.contact_id)
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}


@router.get("/v1/array-owners/ops/tickets")
def list_tickets_ep(
    status: Optional[str] = None,
    array_id: Optional[int] = None,
    active_only: int = 0,
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        tickets = list_tickets(
            db, tenant.id, status=status, array_id=array_id,
            active_only=bool(active_only),
        )
        return {
            "ok": True,
            "tickets": [serialize_ticket(t) for t in tickets],
            "summary": summarize_tickets(tickets),
        }


@router.post("/v1/array-owners/ops/tickets")
def create_ticket_ep(body: TicketCreateBody, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        try:
            ticket = open_ticket(
                db, t,
                array_id=body.array_id,
                inverter_id=body.inverter_id,
                contact_id=body.contact_id,
                fail_type=body.fail_type or "other",
                title=body.title,
                description=body.description,
                severity=body.severity or "critical",
                source="manual",
            )
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "ticket": serialize_ticket(ticket)}


@router.patch("/v1/array-owners/ops/tickets/{ticket_id}")
def patch_ticket_ep(
    ticket_id: int,
    body: TicketPatchBody,
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        ticket = _get_ticket(db, t, ticket_id)
        sched = None
        clear_sched = False
        if body.scheduled_for is not None:
            if body.scheduled_for.strip() == "":
                clear_sched = True
            else:
                from datetime import datetime
                try:
                    sched = datetime.fromisoformat(body.scheduled_for.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    raise HTTPException(400, "scheduled_for must be ISO datetime")
        try:
            update_ticket(
                db, t, ticket,
                status=body.status,
                contact_id=body.contact_id,
                tech_note=body.tech_note,
                description=body.description,
                scheduled_for=sched,
                clear_scheduled=clear_sched,
            )
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "ticket": serialize_ticket(ticket)}


@router.post("/v1/array-owners/ops/tickets/{ticket_id}/checkin")
def send_checkin_ep(
    ticket_id: int,
    body: CheckInSendBody = CheckInSendBody(),
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        ticket = _get_ticket(db, t, ticket_id)
        try:
            row = send_checkin(
                db, t, ticket, via="manual",
                to_override=body.to,
                subject_override=body.subject,
                body_override=body.body,
            )
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
        return {"ok": True, "checkin": serialize_checkin(row), "ticket": serialize_ticket(ticket)}


@router.post("/v1/array-owners/ops/tickets/{ticket_id}/note")
def note_ep(
    ticket_id: int,
    body: NoteBody,
    authorization: str | None = Header(default=None),
) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        ticket = _get_ticket(db, t, ticket_id)
        try:
            row = log_inbound_note(db, t, ticket, body.note, via="manual")
            db.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "checkin": serialize_checkin(row), "ticket": serialize_ticket(ticket)}


@router.get("/v1/array-owners/ops/tickets/{ticket_id}/checkins")
def list_checkins_ep(ticket_id: int, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        _get_ticket(db, t, ticket_id)
        rows = db.execute(
            select(RepairCheckIn)
            .where(
                RepairCheckIn.tenant_id == tenant.id,
                RepairCheckIn.ticket_id == ticket_id,
            )
            .order_by(RepairCheckIn.created_at.desc())
            .limit(100)
        ).scalars().all()
        return {"ok": True, "checkins": [serialize_checkin(r) for r in rows]}


@router.post("/v1/array-owners/ops/settings")
def settings_ep(body: SettingsBody, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if body.checkin_mode is not None:
            mode = body.checkin_mode.strip().lower()
            if mode not in VALID_CHECKIN_MODES:
                raise HTTPException(400, "checkin_mode must be off|manual|auto|delay")
            t.repair_checkin_mode = mode
        if body.checkin_hours is not None:
            t.repair_checkin_hours = max(6, min(336, int(body.checkin_hours)))
        if body.auto_open is not None:
            t.repair_auto_open = bool(body.auto_open)
        db.commit()
        return {
            "ok": True,
            "settings": {
                "checkin_mode": effective_checkin_mode(t),
                "checkin_hours": checkin_interval_hours(t),
                "auto_open": bool(t.repair_auto_open),
            },
        }
