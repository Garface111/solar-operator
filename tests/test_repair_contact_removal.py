"""Removing a tech actually removes them — and nothing keeps emailing.

Regression cover for the prod failure where the owner asked Energy Agent to
"delete Rex" and the agent burned its whole round budget without being able to:

  1. `soft_delete_contact` only flipped active/deleted_at. Array assignments
     survived and open tickets kept a live `next_checkin_at`. The sweep then
     re-resolved the contact to None and `build_checkin_draft` fell through to
     the OWNER's own address — removal quietly redirected the tech letters to
     the owner instead of stopping them.
  2. Auto follow-ups had no ceiling; one prod ticket reached "check-in #23".
  3. The Energy Agent had no removal tool at all, so it could not honor the ask.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

import api.repair_ops as ro
from api.db import SessionLocal, init_db
from api.models import (
    Array,
    ArrayServiceAssignment,
    Inverter,
    RepairCheckIn,
    RepairTicket,
    Tenant,
    now,
)


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _tenant(**over) -> str:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    fields = dict(
        id=tid, name="Ops Owner", contact_email=f"{key}@owner.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
        repair_checkin_mode="auto", repair_checkin_hours=48, repair_auto_open=True,
    )
    fields.update(over)
    with SessionLocal() as db:
        db.add(Tenant(**fields))
        db.commit()
    return tid


def _array(tenant_id: str, name: str) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name)
        db.add(arr)
        db.flush()
        aid = arr.id
        db.commit()
    return aid


def _inv(tid, aid, *, name="Inv 1") -> int:
    with SessionLocal() as db:
        iv = Inverter(
            tenant_id=tid, array_id=aid, vendor="solaredge",
            serial="SN-" + secrets.token_hex(3), position=0,
            name=name, model="SE10K", nameplate_kw=10.0,
        )
        db.add(iv)
        db.flush()
        rid = iv.id
        db.commit()
    return rid


# ── removal stands everything down ───────────────────────────────────────────

def test_removal_unassigns_arrays_and_stops_outreach():
    tid = _tenant()
    aid = _array(tid, "Johnson Farm")
    iid = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        rex = ro.upsert_contact(db, tid, name="Rex", email="rex@om.test", is_default=True)
        db.flush()
        ro.assign_array_contact(db, tid, aid, rex.id, kind="primary")
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        rex_id, ticket_id = rex.id, ticket.id

    # A live outreach schedule and a "waiting on the tech" state, as in prod.
    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        ticket.status = "waiting_reply"
        ticket.next_checkin_at = now() - timedelta(hours=1)
        db.commit()

    with SessionLocal() as db:
        summary = ro.soft_delete_contact(db, tid, rex_id)
        db.commit()

        assert summary["name"] == "Rex"
        assert summary["arrays_unassigned"] == 1
        assert summary["tickets_stood_down"] == [ticket_id]

        # Gone from the roster, including the include_inactive view.
        assert ro.get_contact(db, tid, rex_id) is None
        assert ro.list_contacts(db, tid, include_inactive=True) == []

        # No array coverage left behind.
        rows = db.execute(
            select(ArrayServiceAssignment).where(
                ArrayServiceAssignment.contact_id == rex_id,
            )
        ).scalars().all()
        assert rows == []

        # The case is stood down, not still "waiting" on a person who is gone.
        ticket = db.get(RepairTicket, ticket_id)
        assert ticket.next_checkin_at is None
        assert ticket.contact_id is None
        assert ticket.status == "open"
        assert "removed from the roster" in (ticket.tech_note or "")


def test_removed_contact_never_redirects_checkins_to_the_owner():
    """The bug with teeth: after removal the sweep used to mail the OWNER a
    letter addressed to a tech, on the check-in cadence, indefinitely."""
    tid = _tenant()
    aid = _array(tid, "Tannery Brook")
    iid = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        rex = ro.upsert_contact(db, tid, name="Rex", email="rex@om.test", is_default=True)
        db.flush()
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        rex_id, ticket_id = rex.id, ticket.id

    # One approved send, so auto follow-ups are legitimately armed.
    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ):
        t = db.get(Tenant, tid)
        ro.send_checkin(db, t, db.get(RepairTicket, ticket_id), via="agent")
        db.commit()

    with SessionLocal() as db:
        ro.soft_delete_contact(db, tid, rex_id)
        db.commit()

    # Force a due schedule the way a stale row would have survived removal.
    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        ticket.next_checkin_at = now() - timedelta(hours=1)
        db.commit()

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ) as mock_send:
        t = db.get(Tenant, tid)
        sent = ro.process_due(db, t)
        db.commit()
        assert sent == 0
        assert not mock_send.called, "removed tech's ticket must not email anyone"

    with SessionLocal() as db:
        assert db.get(RepairTicket, ticket_id).next_checkin_at is None


def test_send_checkin_auto_refuses_a_ticket_with_no_tech():
    tid = _tenant()
    aid = _array(tid, "Orphan Site")
    iid = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Rex", email="rex@om.test", is_default=True)
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        ticket_id = ticket.id

    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        ticket.contact_id = None
        db.commit()

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ) as mock_send:
        t = db.get(Tenant, tid)
        with pytest.raises(ValueError, match="no service contact"):
            ro.send_checkin(db, t, db.get(RepairTicket, ticket_id), via="auto")
        assert not mock_send.called


# ── the endless-nagging ceiling ──────────────────────────────────────────────

def test_auto_checkins_stop_at_the_ceiling():
    """Prod reached 'check-in #23' on one ticket. Past the ceiling the agent
    stops nagging and the case waits on an owner decision."""
    tid = _tenant()
    aid = _array(tid, "Nag Site")
    iid = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Rex", email="rex@om.test", is_default=True)
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        ticket_id = ticket.id

    # Pre-log exactly the ceiling's worth of successful outbound check-ins.
    with SessionLocal() as db:
        for i in range(ro.MAX_AUTO_CHECKINS):
            db.add(RepairCheckIn(
                tenant_id=tid, ticket_id=ticket_id, channel="email",
                direction="outbound", sent_ok=True, subject=f"check-in {i + 1}",
                body="...",
            ))
        ticket = db.get(RepairTicket, ticket_id)
        ticket.checkin_count = ro.MAX_AUTO_CHECKINS
        ticket.status = "waiting_reply"
        ticket.next_checkin_at = now() - timedelta(hours=1)
        db.commit()

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ) as mock_send:
        t = db.get(Tenant, tid)
        sent = ro.process_due(db, t)
        db.commit()
        assert sent == 0
        assert not mock_send.called

    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        assert ticket.next_checkin_at is None
        assert "auto follow-ups stopped" in (ticket.tech_note or "")


def test_auto_checkin_still_fires_below_the_ceiling():
    """The ceiling must not break ordinary follow-up."""
    tid = _tenant()
    aid = _array(tid, "Normal Site")
    iid = _inv(tid, aid)
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        ro.upsert_contact(db, tid, name="Rex", email="rex@om.test", is_default=True)
        ticket = ro.open_ticket(db, t, array_id=aid, inverter_id=iid, fail_type="dead")
        db.commit()
        ticket_id = ticket.id

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ):
        t = db.get(Tenant, tid)
        ro.send_checkin(db, t, db.get(RepairTicket, ticket_id), via="agent")
        db.commit()

    # Due, and far enough past the minimum gap to be eligible.
    with SessionLocal() as db:
        ticket = db.get(RepairTicket, ticket_id)
        ticket.next_checkin_at = now() - timedelta(hours=1)
        row = db.execute(
            select(RepairCheckIn)
            .where(RepairCheckIn.ticket_id == ticket_id)
            .order_by(RepairCheckIn.created_at.desc())
        ).scalars().first()
        row.created_at = now() - timedelta(days=7)
        db.commit()

    with SessionLocal() as db, patch.object(
        ro.notify, "send_repair_checkin_email", return_value=True,
    ) as mock_send:
        t = db.get(Tenant, tid)
        sent = ro.process_due(db, t)
        db.commit()
        assert sent == 1
        assert mock_send.called


# ── the agent can actually do it now ─────────────────────────────────────────

def test_energy_agent_exposes_remove_service_contact():
    """The gap that caused the incident: the capability existed over REST and in
    the UI, but the agent had no tool for it, so it thrashed to its round
    ceiling trying to honor 'delete Rex'."""
    import api.energy_agent as ea

    names = {
        (d.get("function") or {}).get("name")
        for d in ea.TOOL_DEFS
        if isinstance(d, dict)
    }
    assert "remove_service_contact" in names
    assert "remove_service_contact" in ea.SKILL_REGISTRY["repairs"]["tools"]

    spec = next(
        d for d in ea.TOOL_DEFS
        if (d.get("function") or {}).get("name") == "remove_service_contact"
    )
    desc = spec["function"]["description"].lower()
    # It has to be findable by the words an owner actually uses.
    for word in ("delete", "remove", "take", "roster"):
        assert word in desc, f"removal tool description should mention {word!r}"
