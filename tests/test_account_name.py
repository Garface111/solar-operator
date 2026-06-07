"""Tests for POST /v1/account/name — update operator personal name.

Since the operator/company split (feat/split-operator-and-company-name,
Jun 7'26), POST /v1/account/name writes ``tenants.operator_name`` only and
LEAVES the legacy ``tenants.name`` column untouched. Company-name writes
go through POST /v1/account/company-name (covered by tests/test_account_namesplit.py).
The response body still returns ``name`` for backward compat with any
client still reading that field — it now reflects the freshly written
operator_name."""
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
    """Read the field that POST /v1/account/name now writes (operator_name)."""
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        return t.operator_name


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
