"""Energy Agent can pull the site owner into a crew email thread.

The tool: when the O&M crew says something only the owner can answer, the agent
CC's the owner on its reply, making one real three-way thread instead of the
agent paraphrasing each side to the other.

What these tests actually protect is TRUTHFULNESS, not the CC header:

  * a three-way thread must never misattribute — the owner's words are the
    owner's, the crew's are the crew's (test_context_labels_each_party)
  * the owner must never be added silently (test_first_loop_in_discloses_itself)
  * once on the thread the owner is never quietly dropped
    (test_owner_stays_on_the_thread_once_looped_in)
  * when the owner answers, the answer must REACH THE CREW — replying only to
    the owner would strand the people waiting on it
    (test_owner_reply_goes_to_the_crew_with_owner_copied)
  * and the agent must not reach for this tool when it can just do its job
    (test_default_is_no_cc)

Live motivation, prod ticket #74 (2026-07-28): the crew wrote "Please let ford
know", and the agent replied "I've passed along that you're visiting the site
today" — having CC'd nobody and told no one. It described an action it had no
ability to take. That is the bug this closes.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.db import SessionLocal, init_db
from api.models import RepairCheckIn, RepairTicket, ServiceContact, Tenant, now
from api import repair_ops

OWNER_EMAIL = "owner@example.test"
TECH_EMAIL = "rex@crew.test"


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


@pytest.fixture()
def world():
    """One tenant, one crew contact, one open ticket mid-conversation."""
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Dyson Swarm", operator_name="Ford",
            contact_email=OWNER_EMAIL,
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, product="array_operator",
        ))
        db.commit()
    with SessionLocal() as db:
        c = ServiceContact(
            tenant_id=tid, name="Rex", email=TECH_EMAIL, active=True,
        )
        db.add(c)
        db.flush()
        t = RepairTicket(
            tenant_id=tid, contact_id=c.id, site_name="Glover",
            inv_name="INV-3", title="Underperforming", status="waiting_reply",
            fail_type="underperforming",
        )
        db.add(t)
        db.commit()
        yield {"tenant_id": tid, "contact_id": c.id, "ticket_id": t.id}


class _Sent:
    """Records every send_repair_checkin_email call."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return True

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


def _run(monkeypatch, world, plan, *, from_email, body="Please advise."):
    """Drive one inbound reply through the conversation with a fixed LLM plan."""
    sent = _Sent()
    monkeypatch.setattr(repair_ops.notify, "send_repair_checkin_email", sent)
    monkeypatch.setattr(
        repair_ops, "plan_repair_email_reply",
        lambda *a, **k: dict(plan),
    )
    with SessionLocal() as db:
        tenant = db.get(Tenant, world["tenant_id"])
        ticket = db.get(RepairTicket, world["ticket_id"])
        out = repair_ops.continue_repair_email_conversation(
            db, tenant, ticket, from_email=from_email,
            inbound_body=body, inbound_subject="Re: Glover",
        )
        db.commit()
    return sent, out


_BASE_PLAN = {
    "send": True,
    "subject": "Re: Glover — repair (INV-3)",
    "body": "Thanks — noted.\n\n[AO-TICKET-1]",
    "status": None,
    "needs_owner": False,
    "loop_in_owner": False,
    "loop_in_reason": None,
    "owner_chat": "Replied to the crew.",
    "reason": "ack",
}


# ── the tool stays off unless asked for ──────────────────────────────────────

def test_default_is_no_cc(monkeypatch, world):
    """Routine crew traffic must not reach the owner. Their quiet inbox is the product."""
    sent, out = _run(monkeypatch, world, _BASE_PLAN, from_email=TECH_EMAIL)
    assert out["sent"] is True
    assert sent.last["to"] == TECH_EMAIL
    assert not sent.last.get("cc")
    assert out["owner_looped_in"] is False
    with SessionLocal() as db:
        assert db.get(RepairTicket, world["ticket_id"]).owner_looped_in_at is None


def test_a_garbled_plan_never_ccs_a_human(monkeypatch, world):
    """A missing/nonsense loop_in_owner must fail CLOSED, not mail somebody."""
    plan = dict(_BASE_PLAN)
    plan.pop("loop_in_owner")
    sent, out = _run(monkeypatch, world, plan, from_email=TECH_EMAIL)
    assert not sent.last.get("cc")
    assert out["owner_looped_in"] is False


# ── looping in ───────────────────────────────────────────────────────────────

def test_loop_in_ccs_the_owner_and_stamps_the_ticket(monkeypatch, world):
    plan = dict(_BASE_PLAN, loop_in_owner=True,
                loop_in_reason="the gate code for site access")
    sent, out = _run(
        monkeypatch, world, plan, from_email=TECH_EMAIL,
        body="We need the gate code. Please let the owner know.",
    )
    assert sent.last["cc"] == [OWNER_EMAIL]
    assert out["owner_newly_looped_in"] is True
    assert out["cc"] == [OWNER_EMAIL]
    with SessionLocal() as db:
        t = db.get(RepairTicket, world["ticket_id"])
        assert t.owner_looped_in_at is not None
        assert "gate code" in (t.owner_loop_reason or "")


def test_first_loop_in_discloses_itself(monkeypatch, world):
    """No silent CC. The crew must be told the owner is now reading."""
    plan = dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="who pays for the part")
    sent, _ = _run(monkeypatch, world, plan, from_email=TECH_EMAIL)
    body = sent.last["body_text"].lower()
    assert "copied" in body or "copying" in body
    assert "owner" in body


def test_disclosure_is_not_duplicated_when_the_model_already_said_it(
    monkeypatch, world
):
    plan = dict(
        _BASE_PLAN, loop_in_owner=True, loop_in_reason="access",
        body="I'm copying Ford, the site owner, so he can confirm access.\n\n[AO-TICKET-1]",
    )
    sent, _ = _run(monkeypatch, world, plan, from_email=TECH_EMAIL)
    assert sent.last["body_text"].lower().count("copied") == 0
    assert sent.last["body_text"].lower().count("copying") == 1


def test_owner_stays_on_the_thread_once_looped_in(monkeypatch, world):
    """Silently dropping someone off a thread they've been reading is its own lie."""
    first = dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="access")
    _run(monkeypatch, world, first, from_email=TECH_EMAIL)

    # A later, perfectly routine reply — the model does NOT ask to loop in again.
    with SessionLocal() as db:  # clear the anti-chatter gap
        for r in db.execute(
            select(RepairCheckIn).where(
                RepairCheckIn.ticket_id == world["ticket_id"])
        ).scalars().all():
            r.created_at = now() - timedelta(hours=2)
        db.commit()

    sent, out = _run(monkeypatch, world, _BASE_PLAN, from_email=TECH_EMAIL,
                     body="Parts arrive Thursday.")
    assert sent.last["cc"] == [OWNER_EMAIL], "owner was dropped off the thread"
    assert out["owner_newly_looped_in"] is False
    # ...and we don't re-announce them every single message.
    assert "copied" not in sent.last["body_text"].lower()


def test_owner_is_never_cced_on_their_own_message(monkeypatch, world):
    """If the owner is the addressee there is nothing to CC — no duplicate."""
    with SessionLocal() as db:
        t = db.get(RepairTicket, world["ticket_id"])
        t.contact_id = None          # no crew contact → agent addresses the owner
        db.commit()
    plan = dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="a decision")
    sent, _ = _run(monkeypatch, world, plan, from_email=OWNER_EMAIL)
    assert sent.last["to"] == OWNER_EMAIL
    assert not sent.last.get("cc")


# ── the owner replying ───────────────────────────────────────────────────────

def test_owner_reply_goes_to_the_crew_with_owner_copied(monkeypatch, world):
    """The owner's answer must REACH the crew, not bounce back to the owner."""
    _run(monkeypatch, world,
         dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="gate code"),
         from_email=TECH_EMAIL)

    sent, out = _run(
        monkeypatch, world, _BASE_PLAN, from_email=OWNER_EMAIL,
        body="Gate code is 1234, go ahead.",
    )
    assert sent.last["to"] == TECH_EMAIL, "owner's answer never reached the crew"
    assert sent.last["cc"] == [OWNER_EMAIL]
    assert out["sender_party"] == "owner"


def test_owner_reply_is_not_rate_limited_away(monkeypatch, world):
    """The 25s anti-chatter gap must not silently swallow the owner's answer.

    This path drops a skipped reply entirely rather than queueing it, so the gap
    applying here would mean the crew simply never hears back.
    """
    _run(monkeypatch, world,
         dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="gate code"),
         from_email=TECH_EMAIL)
    # No clock manipulation: the owner replies immediately, inside the gap.
    sent, out = _run(monkeypatch, world, _BASE_PLAN, from_email=OWNER_EMAIL,
                     body="Code is 1234.")
    assert out["sent"] is True, f"owner answer dropped: {out.get('skipped')}"
    assert sent.last["to"] == TECH_EMAIL


def test_tech_reply_is_still_rate_limited(monkeypatch, world):
    """The exemption is for the owner only — crew chatter stays capped."""
    _run(monkeypatch, world, _BASE_PLAN, from_email=TECH_EMAIL)
    _sent, out = _run(monkeypatch, world, _BASE_PLAN, from_email=TECH_EMAIL,
                      body="Also, one more thing.")
    assert out["sent"] is False
    assert out["skipped"] == "too_soon"


# ── attribution ──────────────────────────────────────────────────────────────

def test_party_identification(world):
    with SessionLocal() as db:
        tenant = db.get(Tenant, world["tenant_id"])
        contact = db.get(ServiceContact, world["contact_id"])
        p = repair_ops._party_for
        assert p(OWNER_EMAIL, tenant, contact) == "owner"
        assert p(TECH_EMAIL, tenant, contact) == "tech"
        assert p("Rex <REX@CREW.TEST>", tenant, contact) == "tech", "display name/case"
        assert p("dispatch@crew.test", tenant, contact) == "other"
        assert p(None, tenant, contact) == "other"
        assert p("agent@agent.arrayoperator.com", tenant, contact) == "agent"


def test_context_labels_each_party(monkeypatch, world):
    """The model must be able to tell who said what, or it will act on the wrong one."""
    _run(monkeypatch, world,
         dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="gate code"),
         from_email=TECH_EMAIL)
    with SessionLocal() as db:
        tenant = db.get(Tenant, world["tenant_id"])
        ticket = db.get(RepairTicket, world["ticket_id"])
        contact = db.get(ServiceContact, world["contact_id"])
        # Seed one inbound from each side.
        for addr, txt in ((TECH_EMAIL, "we need the code"),
                          (OWNER_EMAIL, "code is 1234")):
            db.add(RepairCheckIn(
                tenant_id=tenant.id, ticket_id=ticket.id, channel="email",
                direction="inbound", body=txt, sent_to=addr, sent_ok=True,
                via="inbound_email",
            ))
        db.commit()
        history = repair_ops._recent_checkins(db, ticket.id)
        ctx = repair_ops._build_convo_context(
            ticket, tenant, contact, history,
            from_email=OWNER_EMAIL, inbound_body="code is 1234",
            inbound_subject="Re: Glover",
        )
    assert "SITE OWNER" in ctx and "TECH (Rex)" in ctx
    assert "owner_is_on_this_thread: yes" in ctx
    # The owner's line must be tagged as the owner's, not the crew's.
    owner_line = [l for l in ctx.splitlines() if "code is 1234" in l][0]
    assert "SITE OWNER" in owner_line
    tech_line = [l for l in ctx.splitlines() if "we need the code" in l][0]
    assert "TECH" in tech_line


def test_checkin_records_who_was_copied(monkeypatch, world):
    """The stored history has to match what actually went out."""
    plan = dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="access")
    _run(monkeypatch, world, plan, from_email=TECH_EMAIL)
    with SessionLocal() as db:
        row = db.execute(
            select(RepairCheckIn)
            .where(RepairCheckIn.ticket_id == world["ticket_id"],
                   RepairCheckIn.direction == "outbound")
            .order_by(RepairCheckIn.created_at.desc())
        ).scalars().first()
        assert row.cc_emails == OWNER_EMAIL
        assert row.sent_to == TECH_EMAIL


def test_failed_send_does_not_claim_the_owner_is_on_the_thread(monkeypatch, world):
    """A bounced email must not leave the ticket believing the owner was added."""
    monkeypatch.setattr(
        repair_ops.notify, "send_repair_checkin_email", lambda **kw: False)
    monkeypatch.setattr(
        repair_ops, "plan_repair_email_reply",
        lambda *a, **k: dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="x"),
    )
    with SessionLocal() as db:
        tenant = db.get(Tenant, world["tenant_id"])
        ticket = db.get(RepairTicket, world["ticket_id"])
        out = repair_ops.continue_repair_email_conversation(
            db, tenant, ticket, from_email=TECH_EMAIL,
            inbound_body="need owner input", inbound_subject="Re: Glover",
        )
        db.commit()
    assert out["sent"] is False
    with SessionLocal() as db:
        assert db.get(RepairTicket, world["ticket_id"]).owner_looped_in_at is None


def test_scheduled_checkin_keeps_the_owner_on_the_thread(monkeypatch, world):
    """A cadence follow-up must not continue a three-way thread without the owner.

    send_checkin is a different code path from the conversation reply, and it is
    what fires on the automatic check-in schedule. If it dropped the CC, the crew
    would keep talking in a thread the owner had been reading — and any reply
    would land somewhere the owner can no longer follow.
    """
    _run(monkeypatch, world,
         dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="access"),
         from_email=TECH_EMAIL)

    # Age the thread past send_checkin's 24h auto-cadence floor.
    with SessionLocal() as db:
        for r in db.execute(
            select(RepairCheckIn).where(
                RepairCheckIn.ticket_id == world["ticket_id"])
        ).scalars().all():
            r.created_at = now() - timedelta(hours=48)
        t = db.get(RepairTicket, world["ticket_id"])
        t.last_checkin_at = now() - timedelta(hours=48)
        db.commit()

    sent = _Sent()
    monkeypatch.setattr(repair_ops.notify, "send_repair_checkin_email", sent)
    with SessionLocal() as db:
        tenant = db.get(Tenant, world["tenant_id"])
        ticket = db.get(RepairTicket, world["ticket_id"])
        repair_ops.send_checkin(db, tenant, ticket, via="auto")
        db.commit()
    assert sent.last["to"] == TECH_EMAIL
    assert sent.last["cc"] == [OWNER_EMAIL], "owner dropped from a scheduled check-in"


def test_unusable_owner_address_degrades_quietly(monkeypatch, world):
    """A malformed owner address must not CC garbage, crash, or block the reply.

    tenants.contact_email is NOT NULL, so the real degenerate case is a junk
    value rather than a missing one. The crew reply must still go out; the owner
    simply cannot be added.
    """
    with SessionLocal() as db:
        db.get(Tenant, world["tenant_id"]).contact_email = "not-an-email"
        db.commit()
    plan = dict(_BASE_PLAN, loop_in_owner=True, loop_in_reason="access")
    sent, out = _run(monkeypatch, world, plan, from_email=TECH_EMAIL)
    assert out["sent"] is True, "a bad owner address must not block the crew reply"
    assert not sent.last.get("cc")
    with SessionLocal() as db:
        assert db.get(RepairTicket, world["ticket_id"]).owner_looped_in_at is None
