"""Inbound repair mail must never cross from one operator's fleet to another's.

The realistic hazard is not exotic: ONE O&M company services arrays for several
operators, so the same technician address exists as a ServiceContact under more
than one tenant. The inbound webhook runs with no tenant context at all — it sees
an address and a body and has to decide whose job this is.

Two paths, and they need different guarantees:

  * with an [AO-TICKET-N] reference the answer is exact — ticket ids are globally
    unique, so the reply lands on the one ticket that owns it
  * without one, the sender's address is the only clue, and when that address
    belongs to two operators there IS no right answer. Refusing to match leaves a
    visible unhandled email; guessing files one operator's words into another
    operator's ticket and answers in their name. We refuse.
"""
from __future__ import annotations

import secrets

import pytest

from api.db import SessionLocal, init_db
from api.models import RepairTicket, ServiceContact, Tenant
from api import repair_ops

SHARED_TECH = "dispatch@regional-om.test"


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _tenant(db, label: str) -> str:
    tid = "ten_" + secrets.token_hex(5)
    db.add(Tenant(
        id=tid, name=label, operator_name=label,
        contact_email=f"{label.lower()}@example.test",
        tenant_key="sol_test_" + secrets.token_hex(8),
        plan="comped", active=True, product="array_operator",
    ))
    db.commit()
    return tid


@pytest.fixture()
def two_operators():
    """Two unrelated operators who happen to use the same service company."""
    with SessionLocal() as db:
        a = _tenant(db, "OperatorA")
        b = _tenant(db, "OperatorB")

    made = {}
    for key, tid, site in (("a", a, "Tannery Brook"), ("b", b, "Glover Hill")):
        with SessionLocal() as db:
            contact = ServiceContact(
                tenant_id=tid, name="Regional O&M", email=SHARED_TECH, active=True,
            )
            db.add(contact)
            db.flush()
            ticket = RepairTicket(
                tenant_id=tid, contact_id=contact.id, site_name=site,
                inv_name="INV-1", title=f"{site} fault", status="waiting_reply",
                fail_type="underperforming",
            )
            db.add(ticket)
            db.commit()
            made[key] = {"tenant_id": tid, "ticket_id": ticket.id}
    return made


def test_ticket_reference_routes_to_the_right_operator(two_operators):
    """The normal case: our own outbound carries [AO-TICKET-N], so the reply is
    unambiguous even though both operators share the technician."""
    with SessionLocal() as db:
        for key in ("a", "b"):
            expected = two_operators[key]
            found = repair_ops.find_ticket_for_inbound(
                db,
                from_email=SHARED_TECH,
                subject=f"Re: work order [AO-TICKET-{expected['ticket_id']}]",
                body="On site now.",
            )
            assert found is not None
            assert found.id == expected["ticket_id"]
            assert found.tenant_id == expected["tenant_id"]


def test_a_shared_technician_without_a_reference_is_not_guessed(two_operators):
    """THE LEAK THIS CLOSES: picking "most recent active ticket" for a shared
    address would file OperatorA's reply against OperatorB's job."""
    with SessionLocal() as db:
        found = repair_ops.find_ticket_for_inbound(
            db, from_email=SHARED_TECH, subject="quick update", body="All fixed.",
        )
    assert found is None


def test_an_unshared_technician_still_matches_by_address(two_operators):
    """The fallback still works where it is safe — one operator, no ambiguity."""
    only_tenant = two_operators["a"]["tenant_id"]
    with SessionLocal() as db:
        solo = ServiceContact(
            tenant_id=only_tenant, name="Solo Electric",
            email="solo@only-one-operator.test", active=True,
        )
        db.add(solo)
        db.flush()
        ticket = RepairTicket(
            tenant_id=only_tenant, contact_id=solo.id, site_name="Chester",
            inv_name="INV-9", title="Breaker", status="waiting_reply",
            fail_type="offline",
        )
        db.add(ticket)
        db.commit()
        expected = ticket.id

    with SessionLocal() as db:
        found = repair_ops.find_ticket_for_inbound(
            db, from_email="solo@only-one-operator.test", subject="update", body="done",
        )
        assert found is not None and found.id == expected


def test_tenant_scope_wins_when_the_caller_knows_it(two_operators):
    """The per-tenant sync passes its tenant; a shared address must then resolve
    only within that tenant, never to the other operator's ticket."""
    a, b = two_operators["a"], two_operators["b"]
    with SessionLocal() as db:
        found = repair_ops.find_ticket_for_inbound(
            db, from_email=SHARED_TECH, subject="update", body="done",
            tenant_id=a["tenant_id"],
        )
        assert found is not None
        assert found.tenant_id == a["tenant_id"]
        assert found.id == a["ticket_id"]
        assert found.id != b["ticket_id"]


def test_a_reference_belonging_to_another_operator_is_rejected_when_scoped(two_operators):
    """A forwarded or quoted token from another operator's thread must not drag
    a tenant-scoped sync onto someone else's ticket."""
    a, b = two_operators["a"], two_operators["b"]
    with SessionLocal() as db:
        found = repair_ops.find_ticket_for_inbound(
            db,
            from_email=SHARED_TECH,
            subject=f"Re: [AO-TICKET-{b['ticket_id']}]",
            body="quoted from another thread",
            tenant_id=a["tenant_id"],
        )
    assert found is None


def test_ingest_refuses_an_ambiguous_sender(two_operators):
    """End to end through the webhook's entry point: no ticket, no reply, no row."""
    with SessionLocal() as db:
        out = repair_ops.ingest_inbound_email(
            db,
            from_email=SHARED_TECH,
            to_emails=["repairs@agent.arrayoperator.com"],
            subject="status",
            body="Finished the work.",
        )
    assert out["ok"] is False
    assert out["matched"] is False
    assert out["reason"] == "no_ticket"
