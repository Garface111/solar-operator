"""Per-offtaker custom email letter vs master fallback.

Hierarchy:
  1. Draft note (this send)
  2. Subscription.email_letter (exclusive to offtaker)
  3. Tenant master offtaker template
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import (
    Array, BillingReportSubscription, ReportDraft, Tenant,
)
from api.billing.routes import _draft_letter_default, _draft_dict


def _seed(email_letter=None, note=None, master_body=None):
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Letter Co", contact_email=f"{tid}@t.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="standard", active=True,
            offtaker_email_body_template=master_body or (
                "<p>MASTER letter for {{offtaker_name}}</p>"),
            offtaker_email_subject_template="Invoice for {{offtaker_name}}",
        ))
        arr = Array(tenant_id=tid, name="Site A", fuel_type="solar")
        db.add(arr)
        db.flush()
        sub = BillingReportSubscription(
            tenant_id=tid, customer_name="Abigail Ives",
            client_email="abigail@example.com",
            array_id=arr.id, cadence="monthly",
            delivery_mode="approval", send_mode="to_me",
            email_letter=email_letter,
        )
        db.add(sub)
        db.flush()
        d = ReportDraft(
            tenant_id=tid, subscription_id=sub.id,
            status="pending", customer_name="Abigail Ives",
            period_label="2026-06", customer_kwh=100.0, amount_usd=42.5,
            invoice_number="1001", note=note,
        )
        db.add(d)
        db.commit()
        return tid, sub.id, d.id


_FIELDS = {
    "tenant_name": "Letter Co", "tenant_email": "ops@t.test",
    "body_t": "<p>MASTER letter for {{offtaker_name}}</p>",
    "subject_t": "Invoice for {{offtaker_name}}",
    "signoff_t": None, "signoff_name": None,
}


def test_master_fallback_when_no_custom():
    tid, sid, did = _seed(email_letter=None)
    with SessionLocal() as db:
        d = db.get(ReportDraft, did)
        sub = db.get(BillingReportSubscription, sid)
        letter = _draft_letter_default(d, sub, _FIELDS)
        assert letter is not None
        assert letter["source"] == "master"
        assert "MASTER letter" in letter["letter"]

        out = _draft_dict(d, sub=sub, email_fields=_FIELDS)
        assert out["email_is_custom"] is False
        assert out["email_source"] == "master"
        assert out["email_letter_master"]


def test_custom_offtaker_letter_wins_as_default():
    tid, sid, did = _seed(email_letter="Hi Abigail — custom for YOU only.")
    with SessionLocal() as db:
        d = db.get(ReportDraft, did)
        sub = db.get(BillingReportSubscription, sid)
        letter = _draft_letter_default(d, sub, _FIELDS)
        assert letter["source"] == "custom"
        assert letter["letter"] == "Hi Abigail — custom for YOU only."
        assert "MASTER" in letter["master_letter"]

        out = _draft_dict(d, sub=sub, email_fields=_FIELDS)
        assert out["email_is_custom"] is True
        assert out["email_source"] == "custom"
        assert out["email_letter_default"] == "Hi Abigail — custom for YOU only."


def test_draft_note_flags_custom_even_without_sub_letter():
    tid, sid, did = _seed(email_letter=None, note="One-off edit for this send")
    with SessionLocal() as db:
        d = db.get(ReportDraft, did)
        sub = db.get(BillingReportSubscription, sid)
        out = _draft_dict(d, sub=sub, email_fields=_FIELDS)
        assert out["email_is_custom"] is True
        assert out["note"] == "One-off edit for this send"


def test_patch_sets_and_reverts_offtaker_letter(client):
    tid, sid, did = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        key = t.tenant_key

    from api.account import _sign_session
    tok = _sign_session(str(tid))
    hdr = {"Authorization": f"Bearer {tok}"}

    r = client.patch(
        f"/v1/array-operator/billing/drafts/{did}",
        json={"note": "Exclusive letter", "email_letter": "Exclusive letter"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body["draft"]["email_is_custom"] is True

    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        assert sub.email_letter == "Exclusive letter"

    r2 = client.patch(
        f"/v1/array-operator/billing/drafts/{did}",
        json={"note": "", "email_letter": None},
        headers=hdr,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["draft"]["email_is_custom"] is False

    with SessionLocal() as db:
        sub = db.get(BillingReportSubscription, sid)
        assert sub.email_letter is None
        d = db.get(ReportDraft, did)
        assert (d.note or "") == ""
