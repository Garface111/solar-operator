"""Tests for the draft → approve → send report inbox (Paul Bozuwa's workflow).

A drafted report sits in an approval inbox; nothing reaches a real customer until
the operator approves it. The operator can attach the period's GMP utility-invoice
PDF, which rides onto the approved email via delivery's existing attach hook.
"""
from __future__ import annotations

import pathlib
import secrets

from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, BillingReportSubscription, ReportDraft

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"
BASE = "/v1/array-operator/billing"


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Draft Test Operator",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _upload(client, auth, fixture="norwich.xlsx", **form):
    data = (FIX / fixture).read_bytes()
    files = {"file": (fixture, data,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post(f"{BASE}/subscriptions", files=files, data=form,
                       headers={"Authorization": auth})


def _auth(a):
    return {"Authorization": a}


def test_generate_draft_then_appears_in_inbox(client):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    r = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r.status_code == 200, r.text
    d = r.json()["draft"]
    assert d["status"] == "pending"
    assert d["customer_name"] == "Norwich Fire District"
    assert d["has_gmp_pdf"] is False
    # Inbox lists it.
    inbox = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    assert [x["id"] for x in inbox] == [d["id"]]


def test_recomputed_draft_carries_budget_and_calculated_credit(client):
    """Regression: the recompute response (POST .../draft, what the frontend hits after
    a money edit) MUST carry budget_amount_usd + the CALCULATED solar_credit_value — not
    nulls. When they were null the calc panel back-derived a fake rate (budget ÷ kWh, the
    $2.42718/kWh bug) and the email preview collapsed to one row. solar_credit_value is the
    real production×rate value (≠ the budget); the budget lives in amount_usd."""
    _B = "/v1/array-operator/billing"
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx").json()["subscription"]["id"]
    # The genuine calculated total (no budget) — what solar_credit_value must equal.
    base = client.post(f"{_B}/subscriptions/{sub_id}/draft",
                       headers=_auth(auth)).json()["draft"]["amount_usd"]
    assert isinstance(base, (int, float)) and base != 6000.0
    # Operator sets a budget, then the frontend recomputes via the SAME draft endpoint.
    client.patch(f"{_B}/subscriptions/{sub_id}",
                 json={"budget_amount_usd": 6000.0}, headers=_auth(auth))
    d = client.post(f"{_B}/subscriptions/{sub_id}/draft",
                    headers=_auth(auth)).json()["draft"]
    # The recompute response (NOT just the /drafts list) carries both fields now.
    assert d["budget_amount_usd"] == 6000.0
    assert d["amount_usd"] == 6000.0                       # budget overrides the total
    assert d["solar_credit_value"] == base                 # the CALCULATED credit, not the budget
    assert d["solar_credit_value"] != 6000.0
    # And the effective rate the panel would show = credit ÷ kWh = the REAL rate, never
    # budget ÷ kWh (the fake $2.42718 came from dividing the $6000 budget by the kWh).
    fake = d["amount_usd"] / d["customer_kwh"]
    real = d["solar_credit_value"] / d["customer_kwh"]
    assert abs(real - fake) > 1e-6


def test_draft_display_name_follows_live_subscription(client):
    """REGRESSION: renaming the offtaker must update the draft card + email preview.
    _draft_dict follows the LIVE subscription name, not the draft's frozen snapshot
    (bug: edited 'Brtuce' -> 'Bruce Genereaux' but the draft still showed 'Brtuce')."""
    tid, auth = _make_tenant()
    with SessionLocal() as db:
        sub = BillingReportSubscription(
            tenant_id=tid, customer_name="Bruce Genereaux",
            allocation_pct=0.12, billing_model="percent_of_array")
        db.add(sub); db.flush()
        sub_id = sub.id
        db.add(ReportDraft(tenant_id=tid, subscription_id=sub_id,
                           customer_name="Brtuce", status="pending"))  # stale frozen name
        db.commit()
    inbox = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    mine = [x for x in inbox if x["subscription_id"] == sub_id]
    assert mine, "draft missing from inbox"
    assert mine[0]["customer_name"] == "Bruce Genereaux"  # live name, not the frozen 'Brtuce'


def test_generate_draft_is_idempotent_per_period(client):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    d1 = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth)).json()["draft"]
    d2 = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth)).json()["draft"]
    assert d1["id"] == d2["id"]  # same period → reuse the pending draft
    inbox = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    assert len(inbox) == 1


def test_pre_bill_null_draft_is_refreshed_not_duplicated(client):
    """A draft created BEFORE the customer's first utility bill lands has
    invoice_number=NULL (no period). When a bill later arrives and generate_draft
    runs, the now-populated invoice_number must NOT spawn a second pending draft —
    it must REFRESH the placeholder in place. (The Paul Bozuwa / HCT Sun draft-8
    bug: a stale $0 duplicate landed in the approval inbox.)"""
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]

    # The pre-bill placeholder: a pending draft with no period / NULL invoice_number.
    with SessionLocal() as db:
        ph = ReportDraft(tenant_id=tid, subscription_id=sub_id,
                         customer_name="Norwich Fire District", status="pending",
                         invoice_number=None, period_label=None, amount_usd=None)
        db.add(ph)
        db.commit()
        ph_id = ph.id

    # The first bill lands → generate the draft for the real period.
    r = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r.status_code == 200, r.text
    d = r.json()["draft"]

    # Same row, refreshed in place — and now carries the landed period's numbers.
    assert d["id"] == ph_id
    assert d["invoice_number"] == "2026-05"   # was NULL, now the bill's period key
    assert d["amount_usd"] and d["amount_usd"] > 0  # no longer a stale $0 draft
    # Exactly ONE pending draft for the subscription — not a duplicate.
    inbox = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    assert [x["id"] for x in inbox] == [ph_id]


def test_generate_draft_collapses_preexisting_duplicate_pendings(client):
    """Self-heal: if duplicate pending drafts already exist for the subscription
    (the bug's leftovers), generate_draft folds them into ONE and refreshes it,
    leaving a single pending draft for the (subscription, period)."""
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    with SessionLocal() as db:
        for _ in range(2):
            db.add(ReportDraft(tenant_id=tid, subscription_id=sub_id,
                               customer_name="Norwich Fire District",
                               status="pending", invoice_number=None))
        db.commit()
    r = client.post(f"{BASE}/subscriptions/{sub_id}/draft", headers=_auth(auth))
    assert r.status_code == 200, r.text
    inbox = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    assert len(inbox) == 1, inbox  # two placeholders collapsed to one


def test_attach_gmp_pdf_then_present_on_draft(client):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth)).json()["draft"]["id"]
    pdf = b"%PDF-1.4\n%GMP invoice fixture\n"
    r = client.post(f"{BASE}/drafts/{draft_id}/gmp-invoice",
                    files={"file": ("gmp_may.pdf", pdf, "application/pdf")},
                    headers=_auth(auth))
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["has_gmp_pdf"] is True
    assert r.json()["draft"]["gmp_filename"] == "gmp_may.pdf"


def test_attach_rejects_non_pdf(client):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth)).json()["draft"]["id"]
    r = client.post(f"{BASE}/drafts/{draft_id}/gmp-invoice",
                    files={"file": ("notapdf.txt", b"hello", "text/plain")},
                    headers=_auth(auth))
    assert r.status_code == 400


def test_approve_sends_and_attaches_gmp_pdf(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, send_mode="to_client",
                     client_email="nfd@norwich.gov").json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth)).json()["draft"]["id"]
    pdf = b"%PDF-1.4\n%GMP invoice fixture\n"
    client.post(f"{BASE}/drafts/{draft_id}/gmp-invoice",
                files={"file": ("gmp.pdf", pdf, "application/pdf")}, headers=_auth(auth))

    captured = {}
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: captured.update(kw) or True)
    r = client.post(f"{BASE}/drafts/{draft_id}/approve", headers=_auth(auth))
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["status"] == "sent"
    # Went to the customer, and the GMP invoice PDF rode along.
    names = [a.get("filename", "") for a in captured.get("attachments", [])]
    assert any(n.startswith("gmp_utility_bill_") and n.endswith(".pdf")
               for n in names), names
    # Draft is now in the sent list, not pending.
    pending = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    assert pending == []
    sent = client.get(f"{BASE}/drafts?status=sent", headers=_auth(auth)).json()["drafts"]
    assert [x["id"] for x in sent] == [draft_id]


def test_approve_twice_409(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, send_mode="to_client",
                     client_email="nfd@norwich.gov").json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth)).json()["draft"]["id"]
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    assert client.post(f"{BASE}/drafts/{draft_id}/approve", headers=_auth(auth)).status_code == 200
    assert client.post(f"{BASE}/drafts/{draft_id}/approve", headers=_auth(auth)).status_code == 409


def test_dismiss_draft(client):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth)).json()["draft"]["id"]
    assert client.post(f"{BASE}/drafts/{draft_id}/dismiss", headers=_auth(auth)).status_code == 200
    assert client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"] == []


def test_drafts_are_tenant_scoped(client):
    _tid_a, auth_a = _make_tenant()
    _tid_b, auth_b = _make_tenant()
    sub_id = _upload(client, auth_a).json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth_a)).json()["draft"]["id"]
    # Tenant B cannot see or touch A's draft.
    assert client.get(f"{BASE}/drafts", headers=_auth(auth_b)).json()["drafts"] == []
    assert client.post(f"{BASE}/drafts/{draft_id}/approve", headers=_auth(auth_b)).status_code == 404


# ─── scheduler delivery-mode routing (auto-send vs draft-for-approval) ────────

def test_scheduled_approval_mode_drafts_and_notifies_operator(client, monkeypatch):
    """A due scheduled period in approval mode (default) creates a pending draft
    and emails the OPERATOR a review note — it does NOT send to the customer."""
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, send_mode="to_client",
                     client_email="nfd@norwich.gov").json()["subscription"]["id"]
    sent = []
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: sent.append(kw) or True)
    from api.scheduler import deliver_billing_reports
    res = deliver_billing_reports("monthly")
    assert sub_id in res["drafted"], res
    assert sub_id not in res["sent"]
    # No email in this run went to MY customer (approval mode never sends to them).
    assert all("nfd@norwich.gov" not in (m.get("to") or "") for m in sent)
    # A pending draft now exists in MY inbox.
    inbox = client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"]
    assert len(inbox) == 1


def test_scheduled_auto_mode_sends_to_customer(client, monkeypatch):
    """A due scheduled period in auto mode sends straight to the customer (the
    original hands-off behavior) and creates no draft."""
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, send_mode="to_client",
                     client_email="nfd@norwich.gov").json()["subscription"]["id"]
    # Flip this customer to auto-send.
    client.patch(f"{BASE}/subscriptions/{sub_id}",
                 json={"delivery_mode": "auto"}, headers=_auth(auth))
    captured = {}
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: captured.update(kw) or True)
    from api.scheduler import deliver_billing_reports
    res = deliver_billing_reports("monthly")
    assert sub_id in res["sent"], res
    assert sub_id not in res["drafted"]
    # Went to the customer; no draft created.
    assert "nfd@norwich.gov" in (captured.get("to") or "")
    assert client.get(f"{BASE}/drafts", headers=_auth(auth)).json()["drafts"] == []


def test_edited_email_note_rides_the_approved_send(client, monkeypatch):
    """Paul's 'edit a pre-written email' ask: a note PATCHed onto the draft must
    appear in the email body that goes out on Approve & send."""
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, send_mode="to_client",
                     client_email="nfd@norwich.gov").json()["subscription"]["id"]
    draft_id = client.post(f"{BASE}/subscriptions/{sub_id}/draft",
                           headers=_auth(auth)).json()["draft"]["id"]
    note = "Paul custom note - thank you for your business this quarter"
    pr = client.patch(f"{BASE}/drafts/{draft_id}", json={"note": note},
                      headers=_auth(auth))
    assert pr.status_code == 200, pr.text

    captured = {}
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: captured.update(kw) or True)
    r = client.post(f"{BASE}/drafts/{draft_id}/approve", headers=_auth(auth))
    assert r.status_code == 200, r.text
    # The edited note rides the sent email: exact in the text body, and present
    # (HTML-escaped) in the html body.
    assert note in (captured.get("text") or ""), captured.get("text")
    assert "thank you for your business this quarter" in (captured.get("html") or "")
