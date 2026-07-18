"""Offtaker invoice delivery-truth: Resend id stamp + webhook health.

Covers:
  - webhook stamps BillingReportSubscription by client_email
  - webhook stamps by last_resend_email_id when present
  - deliver_subscription stores resend id when mock send sets _last_id
  - list API (_sub_dict) exposes delivery health fields
  - Client path still works (no regression)
"""
from __future__ import annotations

import secrets
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal, init_db
from api.models import Tenant, Client, BillingReportSubscription
from api.billing.routes import _sub_dict
from api import notify


def _tid() -> str:
    return "ten_dt_" + secrets.token_hex(5)


def _seed_tenant_and_sub(
    *,
    client_email: str = "offtaker@birch.test",
    last_resend_email_id: str | None = None,
) -> tuple[str, int]:
    init_db()
    tid = _tid()
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Delivery Truth Co",
            contact_email=f"{tid}@operator.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped",
            active=True,
            product="array_operator",
            subscription_status="comped",
        ))
        db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid,
            customer_name="Birch Offtaker LLC",
            client_email=client_email,
            send_mode="to_client",
            billing_model="percent_of_array",
            formats=["pdf"],
            enabled=True,
            last_resend_email_id=last_resend_email_id,
        )
        db.add(sub)
        db.flush()
        sid = sub.id
        db.commit()
    return tid, sid


# ─── webhook: offtaker by client_email ──────────────────────────────────────

def test_webhook_delivered_stamps_offtaker_by_client_email(client):
    _tid_s, sid = _seed_tenant_and_sub(client_email="reports@birch.test")
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": ["reports@birch.test"], "subject": "June invoice"},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert sid in body.get("matched_subs", [])
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sid)
        assert s.last_delivered_at is not None
        assert s.last_bounced_at is None


def test_webhook_bounce_stamps_offtaker_reason(client):
    _tid_s, sid = _seed_tenant_and_sub(client_email="oops@birch.test")
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.bounced",
        "data": {
            "to": ["oops@birch.test"],
            "bounce": {"message": "Mailbox full"},
        },
    })
    assert resp.status_code == 200
    assert sid in resp.json().get("matched_subs", [])
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sid)
        assert s.last_bounced_at is not None
        assert s.last_bounce_reason == "Mailbox full"


def test_webhook_case_insensitive_client_email(client):
    _tid_s, sid = _seed_tenant_and_sub(client_email="Mixed@Birch.Test")
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": ["mixed@birch.test"]},
    })
    assert sid in resp.json().get("matched_subs", [])


# ─── webhook: prefer Resend email id ────────────────────────────────────────

def test_webhook_stamps_offtaker_by_resend_email_id(client):
    """When data.email_id matches last_resend_email_id, stamp even if To differs."""
    rid = "re_offtaker_" + secrets.token_hex(6)
    _tid_s, sid = _seed_tenant_and_sub(
        client_email="real@birch.test",
        last_resend_email_id=rid,
    )
    # Recipient is a sink/BCC-style address — still match via id.
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {
            "to": ["sink@staging.test"],
            "email_id": rid,
            "subject": "invoice",
        },
    })
    assert resp.status_code == 200
    assert sid in resp.json().get("matched_subs", [])
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sid)
        assert s.last_delivered_at is not None


def test_webhook_id_and_email_both_match_once(client):
    rid = "re_once_" + secrets.token_hex(6)
    _tid_s, sid = _seed_tenant_and_sub(
        client_email="both@birch.test",
        last_resend_email_id=rid,
    )
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": ["both@birch.test"], "email_id": rid},
    })
    assert resp.json().get("matched_subs", []).count(sid) == 1


# ─── Client path still works ────────────────────────────────────────────────

def test_webhook_still_stamps_client_by_contact_email(client):
    init_db()
    tid = _tid()
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Client Path Co",
            contact_email="agent@clientpath.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, subscription_status="comped",
        ))
        db.flush()
        c = Client(tenant_id=tid, name="Gen Client",
                   contact_email="gen@clientpath.test", active=True)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": ["gen@clientpath.test"]},
    })
    assert cid in resp.json()["matched"]
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.last_delivered_at is not None


# ─── notify last_id ─────────────────────────────────────────────────────────

def test_send_via_resend_sets_last_id_on_success(monkeypatch):
    # Module-level constant is read at import; patch it (not just env).
    monkeypatch.setattr(notify, "RESEND_API_KEY", "re_test_key")
    monkeypatch.delenv("EMAIL_DRY_RUN", raising=False)
    monkeypatch.delenv("EMAIL_SINK_TO", raising=False)

    class _Emails:
        @staticmethod
        def send(params):
            return {"id": "re_abc123"}

    fake_resend = SimpleNamespace(Emails=_Emails, api_key=None)
    import sys
    sys.modules["resend"] = fake_resend  # type: ignore[assignment]

    ok = notify._send_via_resend(
        to="a@b.test", subject="hi", html="<p>x</p>", text="x",
    )
    assert ok is True
    assert notify.last_resend_id() == "re_abc123"
    assert getattr(notify._send_via_resend, "_last_id", None) == "re_abc123"


def test_send_via_resend_dry_run_no_fake_id(monkeypatch):
    monkeypatch.setenv("EMAIL_DRY_RUN", "1")
    notify._send_via_resend._last_id = "stale"
    ok = notify._send_via_resend(
        to="a@b.test", subject="hi", html="<p>x</p>", text="x",
    )
    assert ok is True
    assert notify.last_resend_id() is None


def test_send_via_resend_fail_clears_id(monkeypatch):
    monkeypatch.delenv("EMAIL_DRY_RUN", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    notify._send_via_resend._last_id = "stale"
    ok = notify._send_via_resend(
        to="a@b.test", subject="hi", html="<p>x</p>", text="x",
    )
    assert ok is False
    assert notify.last_resend_id() is None


# ─── deliver_subscription stamps id ─────────────────────────────────────────

def test_deliver_stores_resend_id_when_mock_sets_last_id(client, monkeypatch):
    """After successful non-test send, sub.last_resend_email_id + result fields."""
    tid, auth = _make_tenant_auth()
    sub_id = _upload_sub(client, auth)

    rid = "re_deliver_" + secrets.token_hex(4)

    def fake_send(**kw):
        # last_resend_id() reads api.notify._send_via_resend._last_id
        notify._send_via_resend._last_id = rid
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    # When the name is replaced, last_resend_id getattr's the fake — set there too.
    fake_send._last_id = rid

    r = client.post(
        f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
        params={"test": "false"},
        headers={"Authorization": auth},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # send-now may nest under "result" or return flat — accept either.
    result = body.get("result") or body
    assert result.get("ok") is True or body.get("ok") is True
    # Prefer nested then top-level.
    resend_id = result.get("resend_email_id") or body.get("resend_email_id")
    status = result.get("delivery_status") or body.get("delivery_status")
    assert resend_id == rid
    assert status == "accepted"

    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        assert s.last_resend_email_id == rid
        # Must NOT claim delivered without Resend delivered event.
        assert s.last_delivered_at is None
        assert s.last_sent_at is not None


def test_deliver_test_send_returns_id_but_does_not_stamp(client, monkeypatch):
    tid, auth = _make_tenant_auth()
    sub_id = _upload_sub(client, auth)
    rid = "re_testonly_" + secrets.token_hex(4)

    def fake_send(**kw):
        fake_send._last_id = rid
        notify._send_via_resend._last_id = rid
        return True

    monkeypatch.setattr("api.notify._send_via_resend", fake_send)
    fake_send._last_id = rid

    r = client.post(
        f"/v1/array-operator/billing/subscriptions/{sub_id}/send-now",
        params={"test": "true"},
        headers={"Authorization": auth},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    result = body.get("result") or body
    resend_id = result.get("resend_email_id") or body.get("resend_email_id")
    # Test may still return id if available
    if resend_id is not None:
        assert resend_id == rid
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        # is_test must not stamp delivery fields on the sub
        assert s.last_resend_email_id is None
        assert s.last_sent_at is None


# ─── _sub_dict exposure ─────────────────────────────────────────────────────

def test_sub_dict_exposes_delivery_fields():
    sub = SimpleNamespace(
        id=1, customer_name="X", client_id=None, array_id=None,
        utility_account_id=None, allocation_pct=None, array_allocations=None,
        array_share_pct=None, crosscheck_threshold_pct=None,
        billing_model="percent_of_array", rate_per_kwh=None,
        discount_pct=None, net_rate_per_kwh=None, auto_attach_gmp=True,
        cadence="monthly", annual_trueup=False, delivery_mode="approval",
        send_mode="to_client", client_email="a@b.test", cc_emails=None,
        operator_email=None, formats=["pdf"], include_summary=False,
        enabled=True, source_filename=None,
        last_sent_at=datetime(2026, 7, 1, 12, 0, 0),
        next_send_at=None, last_sent_period_end=None, last_sent_amount_usd=12.5,
        last_invoice_number="2026-06", invoice_number_start=None,
        invoice_number_next=None, budget_amount_usd=None, parsed_map=None,
        last_resend_email_id="re_xyz",
        last_delivered_at=datetime(2026, 7, 1, 12, 5, 0),
        last_bounced_at=None,
        last_bounce_reason=None,
    )
    d = _sub_dict(sub)
    assert d["last_resend_email_id"] == "re_xyz"
    assert d["last_delivered_at"] == "2026-07-01T12:05:00"
    assert d["last_bounced_at"] is None
    assert d["last_bounce_reason"] is None


def test_list_api_includes_delivery_fields(client):
    tid, auth = _make_tenant_auth()
    sub_id = _upload_sub(client, auth)
    with SessionLocal() as db:
        s = db.get(BillingReportSubscription, sub_id)
        s.last_resend_email_id = "re_list_1"
        s.last_delivered_at = datetime(2026, 7, 2, 9, 0, 0)
        db.commit()
    lst = client.get(
        "/v1/array-operator/billing/subscriptions",
        headers={"Authorization": auth},
    ).json()
    rows = lst.get("subscriptions") or lst
    row = next(x for x in rows if x["id"] == sub_id)
    assert row["last_resend_email_id"] == "re_list_1"
    assert row["last_delivered_at"] is not None
    assert "last_bounced_at" in row
    assert "last_bounce_reason" in row


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_tenant_auth() -> tuple[str, str]:
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


def _upload_sub(client, auth) -> int:
    import pathlib
    fix = pathlib.Path(__file__).parent / "fixtures" / "billing" / "norwich.xlsx"
    data = fix.read_bytes()
    r = client.post(
        "/v1/array-operator/billing/subscriptions",
        files={"file": ("norwich.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"send_mode": "to_client", "client_email": "nfd@norwich.gov",
              "formats": "pdf"},
        headers={"Authorization": auth},
    )
    assert r.status_code == 200, r.text
    return r.json()["subscription"]["id"]
