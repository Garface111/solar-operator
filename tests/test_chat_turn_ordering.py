"""A chat turn must not re-order itself, and must not hide mail it missed.

Prod ticket #74, 2026-07-28 — the exact transcript this protects against:

    17:54:41.310  inbound   Rex: "I'll do this today. Please let ford know"
    17:54:41.338  chat card mirroring that reply        (28ms — propagation is fine)
    17:54:46.879  outbound  the agent's reply to Rex
    17:54:47.365446  user       "did rex reply?"
    17:54:47.365450  assistant  "No reply yet …"

The user row and the assistant row are FOUR MICROSECONDS apart because BOTH were
inserted at the end of the turn — `created_at` was persist time, not ask time.
So a question typed BEFORE Rex's email got stamped AFTER it, the transcript
re-sorted, and the agent appeared to answer "no reply yet" directly underneath
the reply. The answer was true when it was asked; only the ordering lied.

Two independent guarantees here:
  1. the question is stamped when it ARRIVED (test_user_message_*)
  2. mail landing mid-turn is disclosed rather than silently missed
     (test_mid_turn_inbound_*)
"""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import RepairCheckIn, RepairTicket, Tenant, now
from api import energy_agent as ea


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


@pytest.fixture()
def tenant_id():
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Ordering Co", contact_email=f"{tid}@t.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, product="array_operator",
        ))
        db.commit()
    return tid


def _ticket(db, tid, site="Tannery Brook"):
    t = RepairTicket(tenant_id=tid, site_name=site, title="Underperforming",
                     status="waiting_reply", fail_type="underperforming")
    db.add(t)
    db.flush()
    return t


# ── ordering ─────────────────────────────────────────────────────────────────

def test_user_message_is_stamped_when_it_arrived_not_when_the_turn_ended(tenant_id):
    """The bug, reproduced at the storage layer."""
    started = now() - timedelta(seconds=30)      # owner asked 30s ago
    with SessionLocal() as db:
        sess = ea.EaSession(id="s_" + secrets.token_hex(6), tenant_id=tenant_id)
        db.add(sess)
        db.flush()
        # An email card lands mid-turn, 20s after the question.
        db.add(ea.EaMessage(
            session_id=sess.id, tenant_id=tenant_id, role="assistant",
            created_at=started + timedelta(seconds=20),
            content="Update from Rex: I'll do this today.",
        ))
        # The turn finally persists. The QUESTION carries its arrival time...
        db.add(ea.EaMessage(
            session_id=sess.id, tenant_id=tenant_id, role="user",
            created_at=started, content="did rex reply?",
        ))
        # ...the ANSWER carries turn-end, which is honest.
        db.add(ea.EaMessage(
            session_id=sess.id, tenant_id=tenant_id, role="assistant",
            content="No reply yet.",
        ))
        db.commit()
        rows = db.execute(
            select(ea.EaMessage)
            .where(ea.EaMessage.session_id == sess.id)
            .order_by(ea.EaMessage.created_at)
        ).scalars().all()

    order = [r.content[:18] for r in rows]
    assert order[0].startswith("did rex reply"), (
        "the question must sort FIRST — it was asked before the email landed; "
        f"got {order}"
    )
    assert "Update from Rex" in order[1]
    assert order[2].startswith("No reply yet")


def test_turn_passes_an_explicit_created_at_for_the_user_row():
    """Guard the actual call site: a bare insert would re-introduce the bug.

    _agent_turn is far too heavy to invoke here, so assert on the source that
    the user EaMessage is constructed with created_at rather than defaulting.
    """
    import inspect as _inspect
    src = _inspect.getsource(ea._agent_turn)
    assert "turn_started_at = _now()" in src
    marker = 'role="user"'
    i = src.index(marker)
    window = src[i:i + 400]
    assert "created_at=turn_started_at" in window, (
        "the user message must be stamped with turn_started_at, not persist time"
    )


# ── mid-turn mail ────────────────────────────────────────────────────────────

def test_mid_turn_inbound_is_detected(tenant_id):
    started = now()
    with SessionLocal() as db:
        t = _ticket(db, tenant_id)
        # Arrived BEFORE the turn — the turn already knew, must not re-announce.
        db.add(RepairCheckIn(
            tenant_id=tenant_id, ticket_id=t.id, channel="email",
            direction="inbound", body="older reply", sent_to="rex@crew.test",
            sent_ok=True, via="inbound_email",
            created_at=started - timedelta(minutes=5),
        ))
        # Arrived DURING the turn — invisible to the model, must surface.
        db.add(RepairCheckIn(
            tenant_id=tenant_id, ticket_id=t.id, channel="email",
            direction="inbound", body="I'll do this today. Please let ford know",
            sent_to="rex@crew.test", sent_ok=True, via="inbound_email",
            created_at=started + timedelta(seconds=6),
        ))
        db.commit()
        late = ea.repair_mail_landed_since(db, tenant_id, started)

    assert len(late) == 1, f"expected only the mid-turn reply, got {late}"
    assert "let ford know" in late[0]["body"]
    assert late[0]["site"] == "Tannery Brook"


def test_outbound_and_other_tenants_are_not_reported(tenant_id):
    started = now()
    other = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=other, name="Other", contact_email=f"{other}@t.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, product="array_operator",
        ))
        db.commit()
    with SessionLocal() as db:
        t = _ticket(db, tenant_id)
        t2 = _ticket(db, other, site="Elsewhere")
        for tid_, tick, direction in (
            (tenant_id, t, "outbound"),      # our own mail, not a reply
            (other, t2, "inbound"),          # someone else's tenant
        ):
            db.add(RepairCheckIn(
                tenant_id=tid_, ticket_id=tick.id, channel="email",
                direction=direction, body="noise", sent_to="x@y.test",
                sent_ok=True, via="conversation",
                created_at=started + timedelta(seconds=3),
            ))
        db.commit()
        assert ea.repair_mail_landed_since(db, tenant_id, started) == []


def test_no_since_is_survivable(tenant_id):
    with SessionLocal() as db:
        assert ea.repair_mail_landed_since(db, tenant_id, None) == []
