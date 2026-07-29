"""escalate_stale_repairs's owner email says, verbatim, "it's still down... push
harder, or bring in someone else?" — but the query it draws candidates from
never looked at `scheduled_for`, a real structured field set only when a tech
gives a concrete date. A ticket with a booked visit for tomorrow still got
that "still down, want me to push harder?" email today, the same false-alarm
shape as the VT Electric Coop "please log in" bug: an automated message
asserting a stale state instead of the ticket's actual one.

The fix: skip escalation while `scheduled_for` is a real future date. Once
that date passes without resolution it's a genuine stall again, so the
original candidate query (opened_at <= cutoff, no scheduled_for at all) must
keep firing unchanged.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

import pytest

import api.repair_ops as ro
from api.db import SessionLocal, init_db
from api.models import RepairTicket, Tenant, now


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _tenant() -> Tenant:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    t = Tenant(
        id=tid, name="Escalation Test", contact_email=f"{key}@owner.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
    )
    with SessionLocal() as db:
        db.add(t)
        db.commit()
        db.refresh(t)
        db.expunge(t)
    return t


def _stale_ticket(tenant_id: str, **over) -> int:
    fields = dict(
        tenant_id=tenant_id, title="Inverter down", fail_type="fault",
        status="open", opened_at=now() - timedelta(days=10),
    )
    fields.update(over)
    with SessionLocal() as db:
        t = RepairTicket(**fields)
        db.add(t)
        db.commit()
        db.refresh(t)
        tid = t.id
    return tid


def test_ticket_with_future_scheduled_visit_is_not_escalated():
    tenant = _tenant()
    ticket_id = _stale_ticket(
        tenant.id, status="scheduled", scheduled_for=now() + timedelta(days=2),
    )
    with patch("api.energy_agent_email.send_repair_escalation_email") as send:
        with SessionLocal() as db:
            sent = ro.escalate_stale_repairs(db, tenant)
            db.commit()
    assert sent == 0
    send.assert_not_called()
    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        assert ticket.owner_escalated_at is None


def test_ticket_with_missed_appointment_still_escalates():
    tenant = _tenant()
    ticket_id = _stale_ticket(
        tenant.id, status="scheduled", scheduled_for=now() - timedelta(days=1),
    )
    with patch("api.energy_agent_email.send_repair_escalation_email", return_value=True) as send:
        with SessionLocal() as db:
            sent = ro.escalate_stale_repairs(db, tenant)
            db.commit()
    assert sent == 1
    send.assert_called_once()
    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        assert ticket.owner_escalated_at is not None


def test_ticket_with_no_scheduled_date_still_escalates_unchanged():
    tenant = _tenant()
    ticket_id = _stale_ticket(tenant.id, status="open", scheduled_for=None)
    with patch("api.energy_agent_email.send_repair_escalation_email", return_value=True) as send:
        with SessionLocal() as db:
            sent = ro.escalate_stale_repairs(db, tenant)
            db.commit()
    assert sent == 1
    send.assert_called_once()
    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        assert ticket.owner_escalated_at is not None
