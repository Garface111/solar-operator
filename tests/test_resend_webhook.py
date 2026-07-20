"""Tests for the Resend delivery webhook (W2-6).

Posts email lifecycle events to /v1/resend/webhook and asserts per-client
delivery health is persisted. Uses throwaway non-Bruce data.
"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm.exc import StaleDataError

from api.db import SessionLocal, init_db
from api.models import Tenant, Client, now
from api.resend_webhook import _stamp_clients_by_email


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


def _seed_n_clients(contact_email: str, n: int) -> list[int]:
    """N live clients sharing one contact_email (prod race: operator address)."""
    init_db()
    tid = "ten_test_" + secrets.token_hex(6)
    ids: list[int] = []
    with SessionLocal() as db:
        t = Tenant(
            id=tid, name="Shared Email Co", contact_email="agent@shared.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, subscription_status="comped",
        )
        db.add(t)
        db.flush()
        for i in range(n):
            c = Client(
                tenant_id=tid, name=f"Client {i} {secrets.token_hex(3)}",
                contact_email=contact_email, active=True,
            )
            db.add(c)
            db.flush()
            ids.append(c.id)
        db.commit()
    return ids


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


def test_orm_dirty_update_raises_staledata_when_row_vanishes():
    """Documents Sentry PYTHON-FASTAPI-1B failure mode: ORM expects N updates,
    one PK was hard-deleted concurrently → StaleDataError."""
    email = f"stale_{secrets.token_hex(4)}@race.test"
    ids = _seed_n_clients(email, n=5)
    with SessionLocal() as db:
        loaded = db.execute(
            select(Client).where(Client.contact_email == email)
        ).scalars().all()
        assert len(loaded) == 5
        victim = ids[0]
        with SessionLocal() as other:
            other.execute(delete(Client).where(Client.id == victim))
            other.commit()
        ts = now()
        for c in loaded:
            c.last_delivered_at = ts
        with pytest.raises(StaleDataError):
            db.commit()


def test_stamp_helper_survives_concurrent_client_delete():
    """Regression: set-based stamp must not raise when one matched row is gone
    (the bug the ORM dirty path hits above)."""
    email = f"ok_{secrets.token_hex(4)}@race.test"
    ids = _seed_n_clients(email, n=5)
    victim = ids[0]
    with SessionLocal() as db:
        # Load all into the identity map first (old webhook did this, then
        # dirtied every instance — race window until commit).
        preloaded = db.execute(
            select(Client).where(Client.contact_email == email)
        ).scalars().all()
        assert len(preloaded) == 5
        with SessionLocal() as other:
            other.execute(delete(Client).where(Client.id == victim))
            other.commit()
        # Helper must use Core UPDATE and not dirty the vanished ORM instance.
        matched = _stamp_clients_by_email(
            db, email, "email.delivered", None, now()
        )
        db.commit()
    assert victim not in matched
    assert set(matched) == set(ids[1:])
    with SessionLocal() as db:
        for cid in ids[1:]:
            c = db.get(Client, cid)
            assert c.last_delivered_at is not None


def test_webhook_many_clients_same_email_delivered(client):
    """Shared contact_email (magic-link / operator address) stamps all live rows."""
    email = f"many_{secrets.token_hex(4)}@shared.test"
    ids = _seed_n_clients(email, n=10)
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {
            "to": [email],
            "subject": "Sign in to Array Operator",
            "email_id": "test-email-id-many",
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert set(body["matched"]) == set(ids)
    with SessionLocal() as db:
        for cid in ids:
            assert db.get(Client, cid).last_delivered_at is not None


def test_webhook_skips_soft_deleted_clients(client):
    email = f"soft_{secrets.token_hex(4)}@shared.test"
    ids = _seed_n_clients(email, n=3)
    with SessionLocal() as db:
        gone = db.get(Client, ids[0])
        gone.deleted_at = now()
        db.commit()
    resp = client.post("/v1/resend/webhook", json={
        "type": "email.delivered",
        "data": {"to": [email]},
    })
    assert resp.status_code == 200
    matched = resp.json()["matched"]
    assert ids[0] not in matched
    assert set(matched) == set(ids[1:])
