"""The live-update signal: every kind of write must reach the browser."""
from datetime import date

from bankai import events, vault
from bankai.goals import Goal
from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.models import AgentAction, ChatMessage, Document, MemoryNote, Rule
from bankai.watchpoints import cancel_watchpoint, create_watchpoint


def fp(session):
    return events.fingerprints(session)


def test_no_change_reports_nothing(session):
    before = fp(session)
    assert events.changed_topics(before, fp(session)) == []


def test_first_poll_is_silent(session):
    """Page load already fetched everything; the opening poll must not restate it."""
    assert events.changed_topics({}, fp(session)) == []


def test_every_topic_has_a_fingerprint(session):
    assert set(fp(session)) == set(events.TOPICS)


def test_new_chat_message_is_detected(session):
    before = fp(session)
    session.add(ChatMessage(channel="web", role="user", speaker="Ford", content="hi"))
    session.flush()
    assert "chat" in events.changed_topics(before, fp(session))


def test_balance_change_alone_is_detected(session):
    """No row added or removed — only a balance moved. Net worth must still update."""
    upsert_account(session, source="csv", name="Checking", balance=100.0)
    session.flush()
    before = fp(session)
    upsert_account(session, source="csv", name="Checking", balance=250.0)
    session.flush()
    assert "accounts" in events.changed_topics(before, fp(session))


def test_transactions_detected(session):
    account = upsert_account(session, source="csv", name="Checking", balance=0.0)
    session.flush()
    before = fp(session)
    ingest_transactions(session, account, [
        TxnIn(posted=date.today(), amount=-12.0, description="COFFEE")
    ])
    session.flush()
    assert "transactions" in events.changed_topics(before, fp(session))


def test_document_annotation_in_place_is_detected(session, tmp_path, monkeypatch):
    """The copilot rewriting a summary changes no row count — the vault panel
    still has to repaint, or its digests silently go stale on screen."""
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", tmp_path / "documents")
    doc, _ = vault.add_document(session, filename="deed.txt", data=b"deed text")
    session.flush()
    before = fp(session)
    doc.summary = "Grant deed, recorded 2026-02-13; grantors A+B."
    session.flush()
    assert "documents" in events.changed_topics(before, fp(session))


def test_watchpoint_firing_is_detected(session):
    """'armed' and 'fired' are both five characters and the row id never moves —
    a length- or id-based fingerprint would miss this entirely."""
    wp = create_watchpoint(
        session, title="Cash floor", note="n", kind="on_date",
        params={"date": "2026-01-01"},
    )
    session.flush()
    before = fp(session)
    wp.status = "fired"
    session.flush()
    assert "watchpoints" in events.changed_topics(before, fp(session))


def test_watchpoint_cancel_is_detected(session):
    wp = create_watchpoint(
        session, title="x", note="n", kind="on_date", params={"date": "2030-01-01"}
    )
    session.flush()
    before = fp(session)
    cancel_watchpoint(session, wp.id)
    session.flush()
    assert "watchpoints" in events.changed_topics(before, fp(session))


def test_goal_status_change_is_detected(session):
    goal = Goal(name="Emergency fund", target_amount=30000.0)
    session.add(goal)
    session.flush()
    before = fp(session)
    goal.status = "reached"  # same length as 'active'? no — but status-only change
    session.flush()
    assert "goals" in events.changed_topics(before, fp(session))


def test_rule_disable_is_detected(session):
    rule = Rule(name="Low balance", kind="balance_below", params={"threshold": 100})
    session.add(rule)
    session.flush()
    before = fp(session)
    rule.enabled = False
    session.flush()
    assert "rules" in events.changed_topics(before, fp(session))


def test_action_approval_is_detected(session):
    action = AgentAction(kind="email_support", title="Cancel PS Plus")
    session.add(action)
    session.flush()
    before = fp(session)
    action.status = "executed"
    session.flush()
    assert "actions" in events.changed_topics(before, fp(session))


def test_memory_edit_is_detected(session):
    note = MemoryNote(title="Household picture", content="v1")
    session.add(note)
    session.flush()
    before = fp(session)
    note.content = "v2 — bigger picture"
    session.flush()
    assert "memories" in events.changed_topics(before, fp(session))


def test_unrelated_topics_stay_quiet(session):
    """A chat message must not repaint the whole dashboard."""
    before = fp(session)
    session.add(ChatMessage(channel="web", role="user", speaker="Ford", content="hi"))
    session.flush()
    changed = events.changed_topics(before, fp(session))
    assert changed == ["chat"]
