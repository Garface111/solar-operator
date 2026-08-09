"""The Saturday-morning printed page: right data, right day, honest delivery."""
import json
from datetime import date, datetime, timedelta

import pytest

from bankai import config, reports, scheduler
from bankai.agent import chat as agent_chat
from bankai.db import session_scope
from bankai.models import Account, ChatMessage, MemoryNote, Transaction


def _seed(session):
    acct = Account(name="Checking", kind="checking", balance=5000.0)
    session.add(acct)
    session.flush()
    today = date.today()
    rows = [
        (today - timedelta(days=2), -120.0, "groceries"),
        (today - timedelta(days=3), -80.0, "dining"),
        (today - timedelta(days=4), 2000.0, "income"),
        (today - timedelta(days=10), -300.0, "groceries"),
        (today - timedelta(days=20), -50.0, "transport"),
        (today - timedelta(days=5), -999.0, "transfer"),  # must be excluded
    ]
    for i, (posted, amount, category) in enumerate(rows):
        session.add(Transaction(
            account_id=acct.id, posted=posted, amount=amount,
            category=category, fingerprint=f"fp-{i}",
        ))
    session.flush()
    return acct


def test_gather_windows_and_excludes_transfers(session):
    _seed(session)
    data = reports.gather_weekly_data(session)
    assert data["this_week"]["spend"] == 200.0          # 120 + 80, no transfer
    assert data["this_week"]["income"] == 2000.0
    assert data["last_week"]["by_category"] == {"groceries": 300.0}
    assert data["this_month"]["spend"] == 550.0          # all three outflows
    assert "transfer" not in data["this_week"]["by_category"]
    assert data["net_worth"]["total"] == 5000.0


def test_render_pdf_survives_model_punctuation(tmp_path, session):
    _seed(session)
    data = reports.gather_weekly_data(session)
    narrative = "Strong week — you're ahead. “Steady” beats flashy… → keep going."
    out = reports.render_pdf(data, narrative, tmp_path / "weekly.pdf")
    raw = out.read_bytes()
    assert raw.startswith(b"%PDF")
    assert len(raw) > 800


def test_print_pdf_builds_the_lp_command(monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode = 0
        stdout = "request id is household-9"
        stderr = ""

    monkeypatch.setattr(
        reports.subprocess, "run", lambda cmd, **k: seen.update(cmd=cmd) or P()
    )
    job = reports.print_pdf(tmp_path / "x.pdf")
    assert job == "request id is household-9"
    assert seen["cmd"][0] == "lp"
    assert seen["cmd"][seen["cmd"].index("-d") + 1] == config.PRINTER_NAME


def test_weekly_action_fires_only_saturday_morning_once(session, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_REPORT_WEEKDAY", 5)
    monkeypatch.setattr(config, "WEEKLY_REPORT_HOUR", 8)
    saturday_9am = datetime(2026, 8, 15, 9, 0)
    assert saturday_9am.weekday() == 5
    assert scheduler.weekly_report_action(session, saturday_9am) == "run"
    assert scheduler.weekly_report_action(session, datetime(2026, 8, 15, 7, 0)) == "skip"
    assert scheduler.weekly_report_action(session, datetime(2026, 8, 16, 9, 0)) == "skip"
    session.add(MemoryNote(title="Last weekly report", content="2026-08-15"))
    session.flush()
    assert scheduler.weekly_report_action(session, saturday_9am) == "skip"
    assert scheduler.weekly_report_action(session, datetime(2026, 8, 22, 9, 0)) == "run"
    monkeypatch.setattr(config, "WEEKLY_REPORT", False)
    assert scheduler.weekly_report_action(session, saturday_9am) == "skip"


def _live_seed():
    with session_scope() as s:
        acct = Account(name="Checking", kind="checking", balance=1000.0)
        s.add(acct)
        s.flush()
        s.add(Transaction(
            account_id=acct.id, posted=date.today() - timedelta(days=1),
            amount=-42.0, category="groceries", fingerprint="wk-live-1",
        ))


def test_run_weekly_report_delivers_even_when_the_printer_is_dead(
    session, monkeypatch, tmp_path
):
    _live_seed()
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        scheduler.agent_chat, "run_turn",
        lambda s, history, channel="web": "A fine week; keep the pace.",
    )
    from bankai.messaging import email_thread
    monkeypatch.setattr(email_thread, "configured", lambda: False)

    def dead_printer(path, title="household weekly report"):
        raise RuntimeError("lp failed (exit 1): printer unreachable")

    monkeypatch.setattr(reports, "print_pdf", dead_printer)
    saturday = datetime(2026, 8, 15, 9, 0)
    result = scheduler.run_weekly_report_once(saturday)
    assert result["status"] == "ran"
    assert result["printed"] is False
    with session_scope() as s:
        stored = s.query(ChatMessage).all()
        marker = s.query(MemoryNote).filter_by(title="Last weekly report").one()
    assert any("printer was unreachable" in m.content for m in stored)
    assert any("A fine week" in m.content for m in stored)
    assert marker.content == "2026-08-15"
    # and the page itself exists for a later reprint
    assert (tmp_path / "weekly-2026-08-15.pdf").exists()


def test_print_tool_reports_a_dead_printer_instead_of_lying(
    session, monkeypatch, tmp_path
):
    from bankai.agent.tools import execute_tool

    _seed(session)
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)

    def dead(path, title="household weekly report"):
        raise RuntimeError("printer unreachable")

    monkeypatch.setattr(reports, "print_pdf", dead)
    result = execute_tool(session, "print_weekly_report", {"summary": "A good week."})
    if isinstance(result, str):
        result = json.loads(result)
    assert result["printed"] is False
    assert "printer unreachable" in result["error"]
    assert (tmp_path / f"weekly-{date.today().isoformat()}.pdf").exists()


def test_print_tool_prints_when_the_printer_answers(session, monkeypatch, tmp_path):
    from bankai.agent.tools import execute_tool

    _seed(session)
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(reports, "print_pdf", lambda path, title="household weekly report": "request id is household-7")
    result = execute_tool(session, "print_weekly_report", {"summary": "A good week."})
    if isinstance(result, str):
        result = json.loads(result)
    assert result["printed"] is True
    assert "household-7" in result["job"]


# --- portal dictation wiring (Ctrl+D) ---------------------------------------
# The dashboard is plain static HTML; this pins the load-bearing hooks so a
# refactor can't silently drop verbal input.

def test_portal_wires_ctrl_d_dictation():
    from bankai import config as _cfg
    html = (_cfg.BASE_DIR / "bankai" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="micbtn"' in html
    assert "webkitSpeechRecognition" in html
    assert 'e.key === "d"' in html          # the Ctrl+D toggle
    assert "e.preventDefault()" in html      # or Chrome bookmarks the page
    assert "interimResults = true" in html   # words appear as you speak


def test_print_page_renders_and_prints_what_the_copilot_writes(
    session, monkeypatch, tmp_path
):
    from bankai.agent.tools import execute_tool

    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        reports, "print_pdf",
        lambda path, title="": f"request id is household-11 [{title}]",
    )
    result = execute_tool(session, "print_page", {
        "title": "Groceries for the week",
        "body": "Eggs — a dozen.\nOat milk.\nSomething green.",
    })
    if isinstance(result, str):
        result = json.loads(result)
    assert result["printed"] is True
    assert "Groceries" in result["job"]
    pdf = tmp_path / "page-groceries-for-the-week.pdf"
    assert pdf.exists() and pdf.read_bytes().startswith(b"%PDF")


def test_print_document_prints_pdf_original_and_falls_back_to_text(
    session, monkeypatch, tmp_path
):
    from bankai import vault
    from bankai.agent.tools import execute_tool
    from bankai.models import Document

    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
    printed = []
    monkeypatch.setattr(
        reports, "print_pdf",
        lambda path, title="": printed.append(str(path)) or "request id is household-12",
    )
    # A stored PDF original prints as-is
    original = tmp_path / "doc_abc__statement.pdf"
    original.write_bytes(b"%PDF-1.4 fake")
    doc_pdf = Document(id="doc_abc", title="Apple Card Statement", sha256="a" * 64,
                       content_text="June statement text")
    # A docx-style doc falls back to its extracted text
    doc_txt = Document(id="doc_def", title="Trust Notes", sha256="b" * 64,
                       content_text="The trust redemption arrives mid-month.")
    session.add_all([doc_pdf, doc_txt])
    session.flush()
    monkeypatch.setattr(
        vault, "stored_path",
        lambda d: original if d.id == "doc_abc" else None,
    )

    r1 = execute_tool(session, "print_document", {"document_id": "doc_abc"})
    r1 = json.loads(r1) if isinstance(r1, str) else r1
    assert r1["printed"] is True and printed[-1] == str(original)

    r2 = execute_tool(session, "print_document", {"document_id": "doc_def"})
    r2 = json.loads(r2) if isinstance(r2, str) else r2
    assert r2["printed"] is True
    assert (tmp_path / "doc-doc_def.pdf").exists()

    r3 = execute_tool(session, "print_document", {"document_id": "doc_nope"})
    r3 = json.loads(r3) if isinstance(r3, str) else r3
    assert "no document" in r3["error"]
