"""ingest_owner_email is the "reply and I'll act" delivery mechanism promised
by four different Energy Agent email types (weekly check-in, gap alert,
escalation, reminder) — every one of them tells the owner to just reply.

"Remove the Energy Agent Sovereign entirely" (463e2ee2) deleted the
_SELF_ADDRESSES set (it existed only to list Sovereign's own mailboxes plus
the owner-agent one) but missed the one remaining reader of it a few lines
below, in ingest_owner_email itself. Every inbound owner reply since then hit
a NameError on `if not frm or frm in _SELF_ADDRESSES:` before ever reaching
tenant resolution — caught by a blanket except at both call sites (the Resend
webhook and the poller fallback), so the failure was completely invisible to
the customer: the webhook still answered 200, no reply email ever went out,
and every "reply and I'll act" promise was false for 100% of owners.

These tests exercise the exact broken line directly (no LLM call needed —
they never get far enough to reach one) and would have caught the crash
immediately: a bare NameError propagates straight through the None checks
below into a test failure, it does not need to be provoked in any special way.
"""
from __future__ import annotations

import secrets

import pytest

from api.db import SessionLocal, init_db
from api.energy_agent_email import ingest_owner_email
from api.models import Tenant


@pytest.fixture(scope="module", autouse=True)
def _init():
    # init_db() is Base.metadata.create_all(): a model only registers a table
    # once its module is actually imported. EaEvent (energy_agent_mind) and
    # EaSession/EaMessage (energy_agent) are all reached via LOCAL imports
    # inside ingest_owner_email's call chain, so they must be imported here
    # first or their tables never get created.
    import api.energy_agent  # noqa: F401
    import api.energy_agent_mind  # noqa: F401
    init_db()


def _mk_tenant(contact_email: str) -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Self Address Test", contact_email=contact_email,
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
            product="array_operator",
        ))
        db.commit()
    return tid


def test_a_genuine_owner_reply_does_not_crash():
    """The exact regression: this must not raise NameError. An unknown sender
    still resolves to a clean, deterministic outcome once past that line."""
    with SessionLocal() as db:
        result = ingest_owner_email(
            db,
            from_email="nobody-we-know@example.com",
            subject="Re: your weekly check-in",
            body="Sounds good, thanks!",
        )
    assert result == {"ok": False, "reason": "unknown_sender"}


def test_a_real_owners_reply_is_recognised(monkeypatch):
    """The positive path: a reply from an actual owner's contact_email must
    get past the self-address guard, past tenant resolution, and reach the
    actual agent turn -- proving the whole chain the bug used to break before
    it ever got this far. _agent_turn itself (a real model call) is stubbed;
    everything before it, including the SQLite writes for session/turn
    bookkeeping, is the real code path."""
    import api.energy_agent as ea

    called = {}

    def fake_turn(db, tenant, session, user_text, context, **kw):
        called["user_text"] = user_text
        return {"reply": "stubbed reply", "tool_calls": []}

    monkeypatch.setattr(ea, "_agent_turn", fake_turn)

    email = f"owner_{secrets.token_hex(4)}@example.com"
    _mk_tenant(email)
    with SessionLocal() as db:
        result = ingest_owner_email(
            db, from_email=email, subject="Re: check-in", body="Yes, please refresh it.",
        )
    # However the turn ends, it must NOT be "unknown_sender" or "self_or_empty"
    # -- both would mean the guard misfired for a real owner -- and it must
    # actually have reached the (stubbed) agent turn.
    assert result.get("reason") not in ("unknown_sender", "self_or_empty")
    assert called.get("user_text") == "Yes, please refresh it."


def test_our_own_mailboxes_are_never_treated_as_an_owner_reply():
    """The guard's actual purpose: never let the agent 'reply' to its own sent
    mail (a bounce, a CC of its own outbound landing back in the inbound feed,
    or a copy of its own message). Both currently-real self mailboxes."""
    with SessionLocal() as db:
        for frm in ("agent@agent.arrayoperator.com", "repairs@agent.arrayoperator.com"):
            result = ingest_owner_email(db, from_email=frm, subject="s", body="b")
            assert result == {"ok": False, "reason": "self_or_empty"}, frm


def test_empty_sender_is_rejected_not_crashed():
    with SessionLocal() as db:
        result = ingest_owner_email(db, from_email="", subject="s", body="b")
    assert result == {"ok": False, "reason": "self_or_empty"}


def test_self_addresses_no_longer_reference_the_removed_sovereign_feature():
    """Sovereign is gone (463e2ee2) -- the restored set should reflect that,
    not resurrect dead mailboxes as a copy-paste of the old literal."""
    from api.energy_agent_email import _SELF_ADDRESSES

    assert not any("sovereign" in addr for addr in _SELF_ADDRESSES)
    assert "agent@agent.arrayoperator.com" in _SELF_ADDRESSES
