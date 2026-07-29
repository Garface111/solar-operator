import email.message
import json
from datetime import date, timedelta

import pytest

from bankai import config
from bankai.agent.tools import execute_tool
from bankai.connectors import email_harvest
from bankai.ingest import TxnIn, ingest_transactions, upsert_account
from bankai.models import AgentAction, Document, SyncLog
from bankai.scheduler import monthly_review_action


@pytest.fixture(autouse=True)
def documents_dir(tmp_path, monkeypatch):
    from bankai import vault
    monkeypatch.setattr(vault, "DOCUMENTS_DIR", tmp_path / "documents")


def make_email(subject, sender, attachments):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Tue, 28 Jul 2026 10:00:00 -0700"
    msg.set_content("see attached")
    for name, data in attachments:
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=name
        )
    return msg.as_bytes()


# --- parsing + categories ---

def test_parse_message_extracts_supported_attachments():
    raw = make_email(
        "Your trust documents", "lawyer@firm.com",
        [("Genereaux_Family_Trust.pdf", b"%PDF fake"),
         ("photo.jpg", b"\xff\xd8"),  # unsupported extension: skipped
         ("notes.txt", b"plain text")],
    )
    parsed = email_harvest._parse_message(raw)
    assert parsed["subject"] == "Your trust documents"
    names = [a["filename"] for a in parsed["attachments"]]
    assert names == ["Genereaux_Family_Trust.pdf", "notes.txt"]


def test_guess_category():
    assert email_harvest.guess_category("Genereaux Family Trust.pdf") == "estate"
    assert email_harvest.guess_category("closing disclosure.pdf your mortgage") == "home"
    assert email_harvest.guess_category("Auto policy renewal") == "insurance"
    assert email_harvest.guess_category("2025 1099-DIV") == "tax"
    assert email_harvest.guess_category("random.pdf") == "other"


# --- harvest into the vault ---

def fixture_messages():
    return [
        {
            "from": "lawyer@firm.com", "subject": "Trust execution copies",
            "date": "Mon, 27 Jul 2026 09:00:00 -0700",
            "attachments": [{"filename": "Family_Trust_Agreement.pdf",
                             "data": b"trust bytes"}],
        },
        {
            "from": "escrow@title.com", "subject": "Closing package",
            "date": "Tue, 28 Jul 2026 09:00:00 -0700",
            "attachments": [{"filename": "closing_disclosure.txt",
                             "data": b"Closing Disclosure. Loan amount $1,160,000."}],
        },
    ]


def test_harvest_files_with_provenance_and_dedupes(session, monkeypatch):
    monkeypatch.setattr(email_harvest, "fetch_messages", lambda q: fixture_messages())
    r1 = email_harvest.harvest(session)
    assert r1["documents_filed"] == 2 and r1["duplicates_skipped"] == 0
    docs = {d.category for d in session.query(Document).all()}
    assert docs == {"estate", "home"}
    trust = session.query(Document).filter(Document.category == "estate").one()
    assert "lawyer@firm.com" in trust.summary and "Trust execution copies" in trust.summary
    # second sweep: everything already known
    r2 = email_harvest.harvest(session)
    assert r2["documents_filed"] == 0 and r2["duplicates_skipped"] == 2
    logs = session.query(SyncLog).filter(SyncLog.source == "email").all()
    assert len(logs) == 2


def test_harvest_tool_dispatch(session, monkeypatch):
    monkeypatch.setattr(email_harvest, "fetch_messages", lambda q: fixture_messages())
    out = json.loads(execute_tool(session, "harvest_email_documents", {}))
    assert out["documents_filed"] == 2
    assert out["filed"][0]["from"] == "lawyer@firm.com"


def test_email_tools_error_clearly_when_unconfigured(session, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    # reading the inbox needs IMAP credentials...
    out = json.loads(execute_tool(session, "search_email", {"query": "trust"}))
    assert "not connected" in out["error"]
    # ...sending names both transports, so the fix is obvious either way
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        email_harvest.send_email("a@b.com", "s", "b")


# --- subscription audit ---

def test_subscription_audit(session):
    checking = upsert_account(
        session, source="csv", name="Checking", kind="checking", balance=0.0
    )
    last = date.today() - timedelta(days=3)
    txns = [
        TxnIn(posted=last - timedelta(days=30 * i), amount=-15.99,
              description="PLAYSTATION NETWORK")
        for i in range(6)
    ] + [
        TxnIn(posted=last - timedelta(days=14 * i), amount=2500.0,
              description="ACME PAYROLL")
        for i in range(8)
    ]
    ingest_transactions(session, checking, txns)
    out = json.loads(execute_tool(session, "subscription_audit", {}))
    subs = out["recurring_outflows"]
    assert len(subs) == 1  # paycheck excluded (income)
    assert subs[0]["likely_subscription"] is True
    assert subs[0]["annualized"] == pytest.approx(15.99 * 12, abs=0.1)


# --- action proposals ---

def test_propose_and_list_actions(session):
    out = json.loads(execute_tool(session, "propose_action", {
        "title": "Cancel PlayStation Plus",
        "rationale": "Ford confirmed in chat he no longer uses it; $15.99/mo = $192/yr.",
        "to_email": "support@playstation.com",
        "subject": "Cancel my PlayStation Plus subscription",
        "body": "Please cancel the subscription on the account under ford.genereaux@gmail.com and confirm in writing.",
    }))
    assert out["proposed"] and "Approve & run" in out["next_step"]
    action = session.get(AgentAction, out["action_id"])
    assert action.status == "proposed" and action.kind == "email_support"
    listed = json.loads(execute_tool(session, "list_actions", {}))
    assert listed[0]["title"] == "Cancel PlayStation Plus"
    assert listed[0]["status"] == "proposed"


# --- monthly review marker ---

def test_monthly_review_marker_logic(session):
    today = date(2026, 8, 1)
    assert monthly_review_action(session, today) == "init"
    from bankai.models import MemoryNote
    session.add(MemoryNote(title="Last monthly review", content="2026-08"))
    session.flush()
    assert monthly_review_action(session, today) == "skip"
    assert monthly_review_action(session, date(2026, 9, 2)) == "run"


# --- send transport: Resend preferred, SMTP fallback ---

def test_resend_is_preferred_and_carries_threading_headers(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(config, "EMAIL_FROM", "copilot@solaroperator.org")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "fallback@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(
        email_harvest.smtplib, "SMTP",
        lambda *a, **k: pytest.fail("SMTP must not be used when Resend is configured"),
    )
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "auth": headers["Authorization"], "body": json})
        return FakeResponse()

    monkeypatch.setattr(email_harvest.httpx, "post", fake_post)
    receipt = email_harvest.send_message(
        to=["a@x.com", "b@x.com"], subject="Re: Question", text="hello",
        headers={"In-Reply-To": "<abc@mail>", "References": "<abc@mail>"},
    )
    assert "Resend" in receipt
    assert captured["url"] == email_harvest.RESEND_URL
    assert captured["auth"] == "Bearer re_test_key"
    assert captured["body"]["from"] == "copilot@solaroperator.org"
    assert captured["body"]["to"] == ["a@x.com", "b@x.com"]
    assert captured["body"]["headers"]["In-Reply-To"] == "<abc@mail>"


def test_falls_back_to_smtp_without_a_resend_key(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "EMAIL_FROM", "")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "copilot@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    sent = {}

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            sent["user"] = user

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(email_harvest.smtplib, "SMTP", lambda *a, **k: FakeSMTP())
    receipt = email_harvest.send_message(
        to=["a@x.com"], subject="Hi", text="body",
        headers={"In-Reply-To": "<abc@mail>"},
    )
    assert "SMTP" in receipt
    assert sent["msg"]["From"] == "copilot@gmail.com"
    assert sent["msg"]["In-Reply-To"] == "<abc@mail>"


def test_send_without_any_transport_is_an_actionable_error(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    assert email_harvest.can_send() is False
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        email_harvest.send_message(to=["a@x.com"], subject="s", text="t")
