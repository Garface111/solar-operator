"""Tests for the Resend delivery webhook (W2-6).

Posts email lifecycle events to /v1/resend/webhook and asserts per-client
delivery health is persisted. Uses throwaway non-Bruce data.
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal, init_db
from api.models import Tenant, Client


def _seed_client(contact_email: str | None) -> tuple[str, int]:
    init_db()
    tid = "ten_test_" + secrets.token_hex(6)
    with SessionLocal() as db:
        t = Tenant(
            id=tid, name="Maple Reporting Co", contact_email="agent@maple.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, subscription_status="comped",
        )
        db.add(t); db.flush()
        c = Client(tenant_id=tid, name="Birch Ridge HOA",
                   contact_email=contact_email, active=True)
        db.add(c); db.flush()
        cid = c.id
        db.commit()
    return tid, cid


def test_delivered_event_stamps_last_delivered(client):
    _tid, cid = _seed_client("reports@birchridge.test")
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": ["reports@birchridge.test"], "subject": "Q2 report"},
    })
    assert resp.status_code == 200
    assert cid in resp.json()["matched"]
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.last_delivered_at is not None
        assert c.last_bounced_at is None


def test_bounced_event_stamps_reason(client):
    _tid, cid = _seed_client("oops@birchridge.test")
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.bounced",
        "data": {
            "to": ["oops@birchridge.test"],
            "bounce": {"message": "Mailbox does not exist"},
        },
    })
    assert resp.status_code == 200
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.last_bounced_at is not None
        assert c.last_bounce_reason == "Mailbox does not exist"


def test_complaint_marks_spam(client):
    _tid, cid = _seed_client("spam@birchridge.test")
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.complained",
        "data": {"to": ["spam@birchridge.test"]},
    })
    assert resp.status_code == 200
    with SessionLocal() as db:
        c = db.get(Client, cid)
        assert c.last_bounce_reason == "Marked as spam"


def test_case_insensitive_match_and_unknown_event(client):
    _tid, cid = _seed_client("Mixed@Birchridge.Test")
    # Recipient casing differs from stored casing — should still match.
    delivered = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": ["mixed@birchridge.test"]},
    })
    assert cid in delivered.json()["matched"]
    # Unknown event types are acknowledged but ignored.
    other = client.post("/v1/resend/webhook", json={
        "type": "email.opened",
        "data": {"to": ["mixed@birchridge.test"]},
    })
    assert other.status_code == 200
    assert other.json()["ignored"] == "email.opened"
