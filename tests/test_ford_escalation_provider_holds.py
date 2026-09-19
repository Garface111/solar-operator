"""Permanent provider access failures must not starve the durable escalation inbox."""
from datetime import datetime, timedelta
import io
import json
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import ford_escalations as esc


@pytest.fixture
def inbox(monkeypatch):
    engine = create_engine("sqlite://")
    esc.EaEscalation.__table__.create(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(esc, "SessionLocal", session)
    monkeypatch.setattr(esc, "XAI_API_KEY", "test-only-key")
    notify = MagicMock()
    monkeypatch.setattr(esc, "send_internal_alert", notify)
    yield session, notify
    engine.dispose()


def seed(session, eid, *, days_ago=1, quiet=False):
    with session() as db:
        db.add(esc.EaEscalation(
            id=eid, tenant_id="tenant_test", tenant_email="owner@example.test",
            session_id="session_test", status="open", summary="Original billing concern",
            user_said="Original user request", priority="urgent", kind="billing",
            agent_notes="Prior operator notes", proposed_plan="Original action plan",
            proposed_fix="Original proposed fix", ford_note="Original Ford note",
            quiet=int(quiet), created_at=datetime.utcnow()-timedelta(days=days_ago),
        ))
        db.commit()


def response(*, needs_ford=False):
    return io.BytesIO(json.dumps({"choices":[{"message":{"content":json.dumps({
        "kind":"how_to", "priority":"normal", "agent_notes":"Triage succeeded",
        "proposed_plan":"Existing resolution verified", "needs_ford":needs_ford,
        "one_line":"Resolved request",
    })}}]}).encode())


@pytest.mark.parametrize("status", [401, 403])
def test_permanent_failure_holds_preserves_content_and_next_item_progresses(inbox, monkeypatch, caplog, status):
    session, notify = inbox
    seed(session, "oldest", days_ago=3)
    seed(session, "next", days_ago=1)
    calls = []
    def urlopen(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise HTTPError(req.full_url, status, "Denied", {},
                io.BytesIO(b"sensitive provider body: team-account-secret"))
        return response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    first = esc.process_open_escalations(limit=1)
    assert first == {"processed":0, "errors":1, "notified":0, "held":1, "batch":1}
    with session() as db:
        held = db.get(esc.EaEscalation, "oldest")
        assert held.status == "needs_ford"
        assert held.priority == "urgent" and held.kind == "billing"
        assert held.summary == "Original billing concern"
        assert held.user_said == "Original user request"
        assert held.proposed_plan == "Original action plan"
        assert held.proposed_fix == "Original proposed fix"
        assert held.ford_note == "Original Ford note"
        assert "Prior operator notes" in held.agent_notes
        assert f"HTTP {status}" in held.agent_notes
        assert "automatic retry stopped" in held.agent_notes
        assert held.worked_at is None and held.resolved_at is None and held.notified_at is None
        assert "team-account-secret" not in str(esc._row_dict(held))
    assert "team-account-secret" not in caplog.text
    notify.assert_not_called()

    second = esc.process_open_escalations(limit=1)
    assert second["processed"] == 1 and second["errors"] == 0
    with session() as db:
        assert db.get(esc.EaEscalation, "next").status == "done"
        assert db.get(esc.EaEscalation, "oldest").status == "needs_ford"
    third = esc.process_open_escalations(limit=1)
    assert third["processed"] == third["errors"] == 0
    assert len(calls) == 2  # no repeat request for held record
    notify.assert_not_called()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_provider_failures_remain_retryable(inbox, monkeypatch, status):
    session, notify = inbox
    seed(session, "transient")
    calls = []
    def urlopen(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise HTTPError(req.full_url, status, "Temporary failure", {}, io.BytesIO(b"try later"))
        return response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    first = esc.process_open_escalations(limit=1)
    assert first["errors"] == 1 and first["held"] == 0
    with session() as db:
        row = db.get(esc.EaEscalation, "transient")
        assert row.status == "open"
        assert "will retry" in row.agent_notes
        assert row.priority == "urgent"
    assert esc.process_open_escalations(limit=1)["processed"] == 1
    assert len(calls) == 2
    notify.assert_not_called()


def test_arbitrary_error_text_is_not_classified_as_permanent(inbox, monkeypatch):
    session, notify = inbox
    seed(session, "untyped")
    monkeypatch.setattr(esc, "_grok_triage", MagicMock(side_effect=RuntimeError("HTTP 403 unrelated processing issue")))
    result = esc.process_open_escalations(limit=1)
    assert result["held"] == 0 and result["errors"] == 1
    with session() as db:
        assert db.get(esc.EaEscalation, "untyped").status == "open"
    notify.assert_not_called()


def test_success_still_notifies_when_attention_requested(inbox, monkeypatch):
    session, notify = inbox
    seed(session, "success")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response(needs_ford=True))
    result = esc.process_open_escalations(limit=1)
    assert result["processed"] == 1 and result["notified"] == 1
    assert result["errors"] == result["held"] == 0
    notify.assert_called_once()
    with session() as db:
        assert db.get(esc.EaEscalation, "success").status == "needs_ford"
        assert db.get(esc.EaEscalation, "success").worked_at is not None


def test_manual_review_notes_are_visible_alongside_existing_plan(inbox, monkeypatch):
    monkeypatch.setattr(esc, "ADMIN_API_KEY", "test-admin")
    html = esc.escalations_board(key="test-admin", x_admin_key=None).body.decode()
    assert 'it.status === "needs_ford" && it.proposed_plan && it.agent_notes' in html
    assert "<b>Notes</b>" in html
    assert "${esc(it.agent_notes)}" in html
    assert "${esc(plan)}" in html
