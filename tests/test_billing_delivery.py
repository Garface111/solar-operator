"""Tests for the Array Operator billing endpoints + delivery pipeline.

Covers: /match preview, subscription create/list/patch/delete, the recipient
slider (to me / to client / to both), format selection, and a dry-run send that
mocks Resend so no real email goes out.
"""
from __future__ import annotations

import pathlib
import secrets
from datetime import date

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant, Client, BillingReportSubscription

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"


def _make_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Billing Test Operator",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(12),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _upload(client, auth, fixture="fairlee.xlsx", **form):
    data = (FIX / fixture).read_bytes()
    files = {"file": (fixture, data,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post("/v1/array-operator/billing/subscriptions",
                       files=files, data=form, headers={"Authorization": auth})


# ─── /match ─────────────────────────────────────────────────────────────────

def test_match_preview_saves_nothing(client):
    _, auth = _make_tenant()
    data = (FIX / "norwich.xlsx").read_bytes()
    r = client.post("/v1/array-operator/billing/match",
                    files={"file": ("norwich.xlsx", data, "application/octet-stream")},
                    headers={"Authorization": auth})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]
    assert body["match"]["customer"]["name"] == "Norwich Fire District"
    assert body["match"]["billing_model"] == "percent_of_array"
    # nothing persisted
    with SessionLocal() as db:
        assert db.execute(select(BillingReportSubscription)).first() is None


def test_match_requires_auth(client):
    data = (FIX / "norwich.xlsx").read_bytes()
    r = client.post("/v1/array-operator/billing/match",
                    files={"file": ("n.xlsx", data, "application/octet-stream")})
    assert r.status_code == 401


# ─── subscription lifecycle ─────────────────────────────────────────────────

def test_create_subscription_links_client_and_defaults_to_me(client):
    tid, auth = _make_tenant()
    r = _upload(client, auth, "fairlee.xlsx", cadence="monthly")
    assert r.status_code == 200, r.text
    sub = r.json()["subscription"]
    assert sub["customer_name"] == "Town of Fairlee"
    assert sub["send_mode"] == "to_me"          # safe default — no customer email yet
    assert sub["cadence"] == "monthly"
    assert sub["next_send_at"]
    # A Client row was created underneath the operator.
    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.tenant_id == tid)).scalar_one()
        assert c.name == "Town of Fairlee"
        s = db.execute(select(BillingReportSubscription)).scalar_one()
        assert s.source_workbook  # workbook bytes stored
        assert s.client_id == c.id


def test_list_and_patch_slider_and_formats(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "fairlee.xlsx").json()["subscription"]["id"]

    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert len(lst["subscriptions"]) == 1

    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"send_mode": "to_both", "client_email": "town@fairlee.gov",
                           "formats": ["pdf", "xlsx"], "cadence": "quarterly"},
                     headers={"Authorization": auth})
    assert r.status_code == 200
    s = r.json()["subscription"]
    assert s["send_mode"] == "to_both"
    assert s["client_email"] == "town@fairlee.gov"
    assert set(s["formats"]) == {"pdf", "xlsx"}
    assert s["cadence"] == "quarterly"


def test_patch_rejects_bad_send_mode(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"send_mode": "to_everyone"},
                     headers={"Authorization": auth})
    assert r.status_code == 400


def test_delete_is_soft(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth).json()["subscription"]["id"]
    assert client.delete(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                         headers={"Authorization": auth}).status_code == 200
    lst = client.get("/v1/array-operator/billing/subscriptions",
                     headers={"Authorization": auth}).json()
    assert lst["subscriptions"] == []


def test_tenant_isolation(client):
    _, auth_a = _make_tenant()
    _, auth_b = _make_tenant()
    sub_id = _upload(client, auth_a).json()["subscription"]["id"]
    # B cannot see or touch A's subscription.
    assert client.get("/v1/array-operator/billing/subscriptions",
                      headers={"Authorization": auth_b}).json()["subscriptions"] == []
    r = client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                     json={"enabled": False}, headers={"Authorization": auth_b})
    assert r.status_code == 404


# ─── preview ────────────────────────────────────────────────────────────────

def test_preview_invoice_pdf_streams(client):
    _, auth = _make_tenant()
    sub_id = _upload(client, auth, "valley_cares.xlsx").json()["subscription"]["id"]
    r = client.get(f"/v1/array-operator/billing/subscriptions/{sub_id}/preview",
                   params={"kind": "invoice", "fmt": "pdf"},
                   headers={"Authorization": auth})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


# ─── delivery (mocked Resend) ───────────────────────────────────────────────

def test_send_now_test_goes_to_operator(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "fairlee.xlsx",
                     send_mode="to_client", client_email="town@fairlee.gov",
                     formats='["pdf","xlsx"]').json()["subscription"]["id"]

    captured = {}

    def fake_send(to, subject, html, text, attachments=None, from_addr=None,
                  reply_to=None, product="nepool"):
        captured.update(to=to, subject=subject, attachments=attachments, product=product)
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "true"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # Test send always goes to the operator, never the customer.
    to = captured["to"]
    to_list = to if isinstance(to, list) else [to]
    assert any("operator.test" in addr for addr in to_list)
    assert all("fairlee.gov" not in addr for addr in to_list)
    # Both formats produced invoice + summary attachments.
    names = [a["filename"] for a in captured["attachments"]]
    assert any(n.endswith("_invoice.pdf") for n in names)
    assert any(n.endswith("_invoice.xlsx") for n in names)
    assert any("summary" in n for n in names)


def test_send_now_live_to_client_stamps_schedule(client, monkeypatch):
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx",
                     send_mode="to_both", client_email="nfd@norwich.gov",
                     formats="pdf").json()["subscription"]["id"]

    captured = {}
    monkeypatch.setattr("api.notify._send_via_resend",
                        lambda **kw: captured.update(kw) or True)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "false"}, headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    # to_both → client is primary, operator cc'd.
    to = captured["to"]
    to_list = to if isinstance(to, list) else [to]
    assert any("norwich.gov" in a for a in to_list)
    # Live send stamps the schedule fields.
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.last_sent_at is not None
        assert s.next_send_at is not None
        assert s.last_invoice_number  # e.g. "2026-05"


def test_send_now_to_client_without_email_errors(client, monkeypatch):
    _, auth = _make_tenant()
    # Fairlee workbook has no customer email; force to_client with none set.
    sub_id = _upload(client, auth, "fairlee.xlsx").json()["subscription"]["id"]
    client.patch(f"/v1/array-operator/billing/subscriptions/{sub_id}",
                 json={"send_mode": "to_client", "client_email": ""},
                 headers={"Authorization": auth})
    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    r = client.post(f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
                    params={"test": "false"}, headers={"Authorization": auth})
    assert r.status_code == 422


def test_scheduler_monthly_billing_delivers(client, monkeypatch):
    """The scheduler job picks up THIS tenant's enabled monthly sub and delivers
    it. (Asserts on our own sub id — the session-scoped test DB accumulates subs
    from other tests, so exact counts aren't meaningful.)"""
    from api import scheduler
    tid, auth = _make_tenant()
    sub_id = _upload(client, auth, "norwich.xlsx", cadence="monthly",
                     send_mode="to_me").json()["subscription"]["id"]

    monkeypatch.setattr("api.notify._send_via_resend", lambda **kw: True)
    result = scheduler.deliver_billing_reports("monthly")
    assert sub_id in result["sent"]
    assert sub_id not in result["failed"]
    # And it stamped the schedule on our sub.
    with SessionLocal() as db:
        assert db.get(BillingReportSubscription, sub_id).last_sent_at is not None
