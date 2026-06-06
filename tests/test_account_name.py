"""Tests for POST /v1/account/name — update tenant display name."""
from __future__ import annotations

import secrets

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant


def _make_tenant(name: str = "Original Name") -> tuple[str, str]:
    """Create a minimal active tenant; return (tenant_id, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name=name,
            contact_email=f"{tid}@example.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            active=True,
            subscription_status="active",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _fetch_name(tid: str) -> str | None:
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        return t.name


class TestUpdateName:
    def test_success(self, client):
        tid, auth = _make_tenant()
        resp = client.post(
            "/v1/account/name",
            json={"name": "New Operator Name"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["name"] == "New Operator Name"
        assert _fetch_name(tid) == "New Operator Name"

    def test_whitespace_trimmed(self, client):
        tid, auth = _make_tenant()
        resp = client.post(
            "/v1/account/name",
            json={"name": "  Padded Name  "},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Padded Name"
        assert _fetch_name(tid) == "Padded Name"

    def test_empty_string_rejected(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/name",
            json={"name": ""},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_whitespace_only_rejected(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/name",
            json={"name": "   "},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_too_long_rejected(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/name",
            json={"name": "x" * 121},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"].lower()

    def test_exactly_120_chars_allowed(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/name",
            json={"name": "a" * 120},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200

    def test_stripe_failure_does_not_abort(self, client, monkeypatch):
        """Stripe sync is best-effort — a failure must not roll back the DB update."""
        import stripe
        monkeypatch.setattr(
            stripe.Customer,
            "modify",
            lambda *a, **kw: (_ for _ in ()).throw(Exception("Stripe down")),
        )
        tid, auth = _make_tenant()
        # Give the tenant a fake stripe_customer_id so the code tries to sync.
        with SessionLocal() as db:
            t = db.get(Tenant, tid)
            t.stripe_customer_id = "cus_fake123"
            db.commit()

        resp = client.post(
            "/v1/account/name",
            json={"name": "Stripe-fail Test"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert _fetch_name(tid) == "Stripe-fail Test"
