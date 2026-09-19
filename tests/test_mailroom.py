"""Mail room + auditor (Sep 2026). Reads frozen invoices, dispatches, payments
and settlements; never recomputes an invoice; the auditor's rules run without
a model and the model layer is patched out here."""
from __future__ import annotations

import secrets
import time
from datetime import date, datetime, timedelta
from unittest.mock import patch

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import (BillingEmailDispatch, BillingReportSubscription, OfftakerAuditRun,
                        OfftakerInvoice, OfftakerPayment, OfftakerSettlement, ReportDraft, Tenant)
from api.billing import mailroom, mailroom_audit
from tests.test_offtaker_payments import _sub, _tenant
from tests.test_norwich_reporting_recovery import _invoice

_B = "/v1/array-operator/billing"


def _auth(t):
    return {"Authorization": f"Bearer {mint_session_for_tenant(t.id)}"}


def _dispatch(db, tid, inv_id, *, status="accepted", resend_id="re_abc", to="clerk@town.test"):
    row = BillingEmailDispatch(
        tenant_id=tid, key=f"invoice:{inv_id}", kind="invoice", status=status,
        attempts=1, resend_email_id=resend_id,
        email={"to": to, "cc": None, "bcc": ["op@owner.test"], "subject": "Your solar credit invoice",
               "html": "<p>Hello <b>clerk</b></p>", "text": "Hello clerk",
               "from_addr": '"Norwich" <hello@arrayoperator.com>', "reply_to": "op@owner.test",
               "attachments": [{"filename": "invoice.pdf", "content": "JVBERi0="}]})
    db.add(row)
    db.flush()
    return row


def _frozen(db, tid, sid, key, **kw):
    inv = _invoice(db, tid, sid, key, **kw)
    inv.snapshot = {"customer_name": "Town of Test", "warnings": [],
                    "computed_invoice": {"kwh": 400, "amount_owed": inv.amount_cents / 100,
                                         "invoice_number": key, "period_start": key + "-01",
                                         "period_end": key + "-28", "net_rate_per_kwh": 0.18398,
                                         "effective_rate_per_kwh": 0.22398, "adder_per_kwh": 0.04,
                                         "rate_is_operator_entered": True, "net_rate_source": "customer",
                                         "kwh_source": "utility_bill", "has_utility_bill": True,
                                         "project_total_kwh": 8000, "allocation_pct": 0.05}}
    inv.render_snapshot = {"variants": {"online": {"to": "clerk@town.test", "subject": "Your solar credit invoice",
                                                   "html": "<p>frozen</p>", "text": "frozen"}},
                           "attachments": [{"filename": "invoice.pdf", "content": "JVBERi0xLjQK"}],
                           "artifact_sha256": {"invoice.pdf": "abc"}, "prepared_at": "2026-07-01T09:00:00Z"}
    db.flush()
    return inv


# ─── board ──────────────────────────────────────────────────────────────────

def test_board_shows_sent_with_envelope_delivery_and_payment(client):
    t = _tenant()
    sid = _sub(t.id, customer_name="Town of Test", client_email="clerk@town.test", send_mode="to_client")
    with SessionLocal() as db:
        inv = _frozen(db, t.id, sid, "2026-06", amount=8960)
        _dispatch(db, t.id, inv.id)
        pay = OfftakerPayment(tenant_id=t.id, subscription_id=sid, invoice_number="2026-06",
                              period_key="2026-06-28", amount_cents=8960, fee_cents=44, status="paid",
                              paid_at=datetime(2026, 7, 5), pay_url="x")
        db.add(pay); db.flush()
        inv.payment_id = pay.id
        db.commit()
    r = client.get(f"{_B}/mailroom", headers=_auth(t))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] and b["counts"]["sent_frozen"] == 1
    s = b["sent"][0]
    assert s["legacy"] is False and s["customer_name"] == "Town of Test"
    assert s["amount_usd"] == 89.6 and s["kwh"] == 400
    assert s["to"] == ["clerk@town.test"] and s["bcc"] == ["op@owner.test"]
    assert s["subject"] == "Your solar credit invoice"
    assert s["attachments"] == ["invoice.pdf"]
    assert s["rate"]["effective_rate_per_kwh"] == 0.22398 and s["rate"]["operator_entered"] is True
    assert s["kwh_source"] == "utility_bill" and s["has_utility_bill"] is True
    assert s["dispatch"]["resend_email_id"] == "re_abc"
    assert s["delivery"]["status"] == "accepted"          # mailer accepted, no receipt yet
    assert s["payment_summary"] == "paid" and s["paid_usd"] == 89.6 and s["outstanding_usd"] == 0
    assert b["counts"]["paid"] == 1 and b["counts"]["collected_usd"] == 89.6


def test_board_going_out_has_drafts_holds_and_scheduled(client):
    t = _tenant()
    s_draft = _sub(t.id, customer_name="Draft Co", client_email="d@x.test", delivery_mode="approval")
    s_held = _sub(t.id, customer_name="Held Co", client_email="h@x.test", delivery_mode="auto")
    s_auto = _sub(t.id, customer_name="Auto Co", client_email="a@x.test", delivery_mode="auto",
                  send_mode="to_client")
    s_off = _sub(t.id, customer_name="Disabled Co", enabled=False)
    with SessionLocal() as db:
        db.add(ReportDraft(tenant_id=t.id, subscription_id=s_draft, customer_name="Draft Co",
                           status="pending", period_label="2026-07-01 → 2026-07-31", amount_usd=42.0,
                           created_at=datetime.utcnow() - timedelta(days=12)))
        _invoice(db, t.id, s_held, "2026-07", status="held")
        db.commit()
    r = client.get(f"{_B}/mailroom", headers=_auth(t))
    assert r.status_code == 200, r.text
    out = {x["customer_name"]: x for x in r.json()["outgoing"]}
    assert out["Draft Co"]["kind"] == "draft" and out["Draft Co"]["when_label"] == "On your approval"
    assert out["Held Co"]["kind"] == "held" and "utility bill" in out["Held Co"]["reason"]
    assert out["Auto Co"]["kind"] == "scheduled" and "auto-send" in out["Auto Co"]["when_label"]
    assert out["Auto Co"]["when"]                     # next 1st at 09:00 UTC
    assert "Disabled Co" not in out
    c = r.json()["counts"]
    assert c["drafts"] == 1 and c["held"] == 1 and c["scheduled"] == 1


def test_board_paused_tenant_says_so(client):
    t = _tenant(sending_paused=True)
    _sub(t.id, customer_name="Auto Co", delivery_mode="auto")
    b = client.get(f"{_B}/mailroom", headers=_auth(t)).json()
    assert b["paused"] is True
    assert b["outgoing"][0]["status"] == "paused"


def test_legacy_sends_are_listed_and_flagged(client):
    t = _tenant()
    sid = _sub(t.id, customer_name="Old Co", client_email="old@x.test", send_mode="to_client")
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sid)
        s.last_sent_at = datetime(2026, 5, 2); s.last_sent_period_end = "2026-04-30"
        s.last_sent_amount_usd = 61.25; s.last_invoice_number = "2026-04"
        db.add(ReportDraft(tenant_id=t.id, subscription_id=sid, customer_name="Old Co", status="sent",
                           period_label="2026-03-01 → 2026-03-31", amount_usd=58.0,
                           sent_at=datetime(2026, 4, 2), invoice_number="2026-03"))
        db.commit()
    b = client.get(f"{_B}/mailroom", headers=_auth(t)).json()
    sent = b["sent"]
    assert len(sent) == 2 and all(x["legacy"] for x in sent)
    assert sent[0]["amount_usd"] == 61.25 and sent[0]["to"] == ["old@x.test"]
    assert sent[1]["amount_usd"] == 58.0
    assert b["counts"]["sent_legacy"] == 2
    b2 = client.get(f"{_B}/mailroom?legacy=0", headers=_auth(t)).json()
    assert b2["sent"] == []


# ─── one invoice ────────────────────────────────────────────────────────────

def test_invoice_detail_email_and_attachment_are_frozen_and_tenant_scoped(client):
    t = _tenant(); other = _tenant()
    sid = _sub(t.id, customer_name="Town of Test")
    with SessionLocal() as db:
        inv = _frozen(db, t.id, sid, "2026-06")
        _dispatch(db, t.id, inv.id)
        db.add(OfftakerSettlement(tenant_id=t.id, invoice_id=inv.id, subscription_id=sid,
                                  request_key="rk1", amount_cents=5000, received_on=date(2026, 7, 9),
                                  actor="ford", method="check", note="check #1041"))
        db.commit(); iid = inv.id
    d = client.get(f"{_B}/mailroom/invoice/{iid}", headers=_auth(t))
    assert d.status_code == 200, d.text
    inv = d.json()["invoice"]
    assert inv["figures"]["effective_rate_per_kwh"] == 0.22398
    assert inv["email"]["html"] == "<p>Hello <b>clerk</b></p>"       # the dispatched payload wins
    assert inv["settlements"][0]["amount_usd"] == 50.0 and inv["settlements"][0]["method"] == "check"
    assert inv["payment_summary"] == "partial" and inv["outstanding_usd"] == 50.0
    assert inv["attachment_files"][0]["filename"] == "invoice.pdf"
    e = client.get(f"{_B}/mailroom/invoice/{iid}/email", headers=_auth(t))
    assert e.status_code == 200 and "clerk" in e.text
    assert "default-src 'none'" in e.headers["content-security-policy"]
    a = client.get(f"{_B}/mailroom/invoice/{iid}/attachment/invoice.pdf", headers=_auth(t))
    assert a.status_code == 200 and a.content.startswith(b"%PDF")
    assert a.headers["content-type"].startswith("application/pdf")
    # Another tenant sees nothing.
    assert client.get(f"{_B}/mailroom/invoice/{iid}", headers=_auth(other)).status_code == 404
    assert client.get(f"{_B}/mailroom/invoice/{iid}/attachment/invoice.pdf", headers=_auth(other)).status_code == 404
    assert client.get(f"{_B}/mailroom/invoice/{iid}/attachment/nope.pdf", headers=_auth(t)).status_code == 404
    assert client.get(f"{_B}/mailroom").status_code in (401, 403)


# ─── auditor rules ──────────────────────────────────────────────────────────

def _payload(**over):
    base = {"tenant": {"sending_paused": False}, "subscriptions": [], "outgoing": [],
            "sent": [], "sent_frozen_total": 0, "reconcile": {}}
    base.update(over)
    return base


def _sent(i, sid, month, amount, **kw):
    x = {"id": i, "legacy": False, "kind": "invoice", "subscription_id": sid, "customer_name": f"Sub {sid}",
         "invoice_number": f"INV-{i}", "status": "accepted", "sent_at": f"2026-{month[-2:]}-02T09:00:00",
         "period_key": month, "period_label": f"{month}-01 → {month}-28", "period_start": f"{month}-01",
         "period_end": f"{month}-28", "amount_usd": amount, "kwh": 400,
         "rate": {"effective_rate_per_kwh": 0.22, "operator_entered": True, "source": "customer"},
         "has_utility_bill": True, "to": ["a@b.test"], "delivery": {"status": "accepted"},
         "payment_summary": "paid", "send_mode": "to_client"}
    x.update(kw)
    return x


def test_rules_catch_duplicate_period_amount_jump_and_bad_rate():
    p = _payload(sent=[
        _sent(1, 7, "2026-05", 80.0),
        _sent(2, 7, "2026-06", 82.0),
        _sent(3, 7, "2026-06", 82.0),                                  # same month twice
        _sent(4, 8, "2026-05", 100.0),
        _sent(5, 8, "2026-06", 260.0),                                 # +160%
        _sent(6, 9, "2026-06", 50.0, rate={"effective_rate_per_kwh": 0.22, "operator_entered": False,
                                           "source": "bill_credit", "note": "inferred from the GMP bill"}),
        _sent(7, 10, "2026-06", 50.0, has_utility_bill=False, kwh_source="daily_csv"),
        _sent(8, 11, "2026-06", 50.0, delivery={"status": "bounced", "reason": "mailbox full"}),
    ])
    codes = {(f["code"], f.get("subscription_id")) for f in mailroom_audit.deterministic_checks(p)}
    assert ("duplicate_period", 7) in codes
    assert ("amount_jump", 8) in codes
    assert ("rate_not_entered", 9) in codes
    assert ("no_utility_bill", 10) in codes
    assert ("bounced", 11) in codes
    sev = {f["code"]: f["severity"] for f in mailroom_audit.deterministic_checks(p)}
    assert sev["duplicate_period"] == "critical" and sev["no_utility_bill"] == "critical"


def test_rules_catch_over_allocation_stale_drafts_and_missing_email():
    now = datetime(2026, 9, 19, 12, 0)
    p = _payload(
        subscriptions=[
            {"subscription_id": 1, "customer_name": "A", "enabled": True, "allocation_pct": 0.6, "arrays": ["Maple"]},
            {"subscription_id": 2, "customer_name": "B", "enabled": True, "allocation_pct": 0.5, "arrays": ["Maple"]},
            {"subscription_id": 3, "customer_name": "C", "enabled": False, "allocation_pct": 0.9, "arrays": ["Maple"]},
        ],
        outgoing=[
            {"kind": "draft", "subscription_id": 1, "customer_name": "A", "period_label": "Jul", "amount_usd": 5,
             "created_at": "2026-09-01T00:00:00", "send_mode": "to_client", "email": "a@x.test"},
            {"kind": "scheduled", "subscription_id": 2, "customer_name": "B", "status": "auto",
             "send_mode": "to_client", "email": None},
            {"kind": "held", "subscription_id": 4, "customer_name": "D", "period_label": "Jun",
             "reason": "Missing settled utility bill", "invoice_id": 44},
        ])
    f = mailroom_audit.deterministic_checks(p, now=now)
    codes = {x["code"] for x in f}
    assert "over_allocated" in codes and "stale_draft" in codes and "missing_email" in codes
    assert "held_invoice" in codes
    over = next(x for x in f if x["code"] == "over_allocated")
    assert over["evidence"]["total_pct"] == 110.0          # disabled C excluded


def test_rules_clean_book_is_quiet():
    p = _payload(sent=[_sent(1, 7, "2026-05", 80.0), _sent(2, 7, "2026-06", 84.0)],
                 subscriptions=[{"subscription_id": 7, "enabled": True, "allocation_pct": 0.25, "arrays": ["Maple"]}],
                 outgoing=[{"kind": "scheduled", "subscription_id": 7, "customer_name": "Sub 7",
                            "status": "approval", "send_mode": "to_client", "email": "a@b.test"}])
    assert mailroom_audit.deterministic_checks(p, now=datetime(2026, 7, 3)) == []


# ─── the run, end to end (model patched out) ───────────────────────────────

def test_audit_run_end_to_end_rules_only(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    t = _tenant()
    sid = _sub(t.id, customer_name="Town of Test", client_email="clerk@town.test", send_mode="to_client")
    with SessionLocal() as db:
        a = _frozen(db, t.id, sid, "2026-06", amount=8000)
        b = _frozen(db, t.id, sid, "2026-07", amount=20000)             # +150%
        _dispatch(db, t.id, a.id, resend_id="re_1"); _dispatch(db, t.id, b.id, resend_id="re_2")
        db.commit()
    with patch("api.billing.mailroom_audit.model_review",
               return_value={"ok": False, "error": "ANTHROPIC_API_KEY not set — model review skipped"}):
        r = client.post(f"{_B}/mailroom/audit", headers=_auth(t))
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]
        run = None
        for _ in range(100):
            run = client.get(f"{_B}/mailroom/audit/{rid}", headers=_auth(t)).json()["run"]
            if run["status"] != "running":
                break
            time.sleep(0.1)
    assert run["status"] == "done", run
    assert run["provider"] == "rules-only" and run["model"] is None
    codes = {f["code"] for f in run["findings"]}
    assert "amount_jump" in codes and "payment_not_tracked" in codes
    assert run["verdict"] in ("caution", "stop")
    assert run["stats"]["sent"] == 2 and run["stats"]["model_ok"] is False
    lst = client.get(f"{_B}/mailroom/audit", headers=_auth(t)).json()
    assert lst["latest"]["id"] == rid and lst["latest"]["finding_count"] == len(run["findings"])
    assert "findings" not in lst["latest"]
    # Another tenant cannot read it.
    assert client.get(f"{_B}/mailroom/audit/{rid}", headers=_auth(_tenant())).status_code == 404


def test_audit_model_findings_merge_and_verdict_escalates(client):
    t = _tenant()
    sid = _sub(t.id, customer_name="Town of Test")
    with SessionLocal() as db:
        inv = _frozen(db, t.id, sid, "2026-06")
        _dispatch(db, t.id, inv.id)
        db.commit(); iid = inv.id
    fake = {"ok": True, "verdict": "stop", "summary": "One invoice went to the wrong person.",
            "findings": [{"severity": "critical", "title": "Wrong recipient", "detail": "clerk@town.test is the fire district",
                          "subscription_id": sid, "invoice_id": iid, "customer_name": "Town of Test",
                          "action": "Re-send to the town clerk"}],
            "model": "claude-test", "provider": "anthropic", "seconds": 1.0}
    with patch("api.billing.mailroom_audit.model_review", return_value=fake):
        rid = client.post(f"{_B}/mailroom/audit", headers=_auth(t)).json()["run_id"]
        for _ in range(100):
            run = client.get(f"{_B}/mailroom/audit/{rid}", headers=_auth(t)).json()["run"]
            if run["status"] != "running":
                break
            time.sleep(0.1)
    assert run["status"] == "done" and run["verdict"] == "stop" and run["model"] == "claude-test"
    assert run["summary"].startswith("One invoice")
    top = run["findings"][0]
    assert top["source"] == "model" and top["severity"] == "critical" and top["invoice_id"] == iid


def test_second_click_returns_the_run_in_flight(client):
    t = _tenant()
    with SessionLocal() as db:
        db.add(OfftakerAuditRun(tenant_id=t.id, status="running", started_at=datetime.utcnow()))
        db.commit()
    r = client.post(f"{_B}/mailroom/audit", headers=_auth(t)).json()
    assert r["already_running"] is True


def test_model_review_bounds_payload_and_parses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    captured = {}

    def fake_call_json(**kw):
        captured.update(kw)
        return {"verdict": "ready", "summary": "Clean.", "findings": [
            {"severity": "low", "title": "x", "detail": "y", "subscription_id": 1, "invoice_id": None,
             "customer_name": None, "action": None}]}

    with patch("api.billing.repro.llm.call_json", side_effect=fake_call_json):
        res = mailroom_audit.model_review(_payload(sent=[_sent(i, 1, "2026-06", 5.0) for i in range(700)]), [])
    assert res["ok"] and res["verdict"] == "ready" and res["findings"][0]["source"] == "model"
    assert captured["schema"] is mailroom_audit.FINDINGS_SCHEMA
    assert len(captured["user_text"]) <= mailroom_audit.MAX_PAYLOAD_CHARS + 5000
