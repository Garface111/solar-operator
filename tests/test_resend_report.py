"""
Tests for POST /v1/account/clients/{id}/resend-report.

Asserts:
- On success: email is attempted via send_workbook_email, client.last_delivery_at
  is updated, response is 200 with ok=True and recipient address.
- On Resend SDK failure: endpoint returns 502 with reason, and the failure is logged.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime

import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.models import Array, Client, Tenant


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_setup() -> tuple[str, int, str]:
    """Create tenant + active client with contact_email + one array.
    Returns (tenant_id, client_id, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Resend Test Co",
            contact_email=f"{tid}@resendtest.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            active=True,
            subscription_status="active",
        ))
        db.commit()

    with SessionLocal() as db:
        c = Client(
            tenant_id=tid,
            name="Resend Client " + secrets.token_hex(3),
            contact_email=f"client-{secrets.token_hex(3)}@resendtest.com",
            active=True,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        cid = c.id

    with SessionLocal() as db:
        db.add(Array(
            tenant_id=tid,
            client_id=cid,
            name="Resend Array " + secrets.token_hex(3),
        ))
        db.commit()

    auth = f"Bearer {mint_session_for_tenant(tid)}"
    return tid, cid, auth


# ─── tests ────────────────────────────────────────────────────────────────────

class TestResendReport:
    def test_success_calls_send_workbook(self, client, monkeypatch):
        """On a successful resend the Resend SDK is invoked via send_workbook_email."""
        _, cid, auth = _make_setup()

        send_calls: list[dict] = []

        def fake_send(**kw):
            send_calls.append(kw)
            return True

        import api.delivery as delivery_mod
        monkeypatch.setattr(delivery_mod, "send_workbook_email", fake_send)

        resp = client.post(
            f"/v1/account/clients/{cid}/resend-report",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["recipient"] != ""
        assert len(send_calls) >= 1

    def test_success_updates_last_delivery_at(self, client, monkeypatch):
        """After a successful resend, client.last_delivery_at is set to now."""
        _, cid, auth = _make_setup()

        import api.delivery as delivery_mod
        monkeypatch.setattr(delivery_mod, "send_workbook_email", lambda **kw: True)

        before = datetime.utcnow()
        resp = client.post(
            f"/v1/account/clients/{cid}/resend-report",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200

        with SessionLocal() as db:
            c = db.get(Client, cid)
            assert c.last_delivery_at is not None
            assert c.last_delivery_at >= before

    def test_email_failure_returns_502(self, client, monkeypatch):
        """When the Resend SDK fails, the endpoint returns 502 (not 200)."""
        _, cid, auth = _make_setup()

        import api.delivery as delivery_mod
        monkeypatch.setattr(delivery_mod, "send_workbook_email", lambda **kw: False)

        resp = client.post(
            f"/v1/account/clients/{cid}/resend-report",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 502

    def test_email_failure_has_reason_in_body(self, client, monkeypatch):
        """502 body carries a human-readable reason for the failure toast."""
        _, cid, auth = _make_setup()

        import api.delivery as delivery_mod
        monkeypatch.setattr(delivery_mod, "send_workbook_email", lambda **kw: False)

        resp = client.post(
            f"/v1/account/clients/{cid}/resend-report",
            headers={"Authorization": auth},
        )
        assert resp.status_code == 502
        detail = resp.json().get("detail", "")
        assert isinstance(detail, str) and len(detail) > 0

    def test_email_failure_is_logged(self, client, monkeypatch, caplog):
        """Email delivery failure produces an ERROR log entry in api.account."""
        _, cid, auth = _make_setup()

        import api.delivery as delivery_mod
        monkeypatch.setattr(delivery_mod, "send_workbook_email", lambda **kw: False)

        with caplog.at_level(logging.ERROR, logger="api.account"):
            client.post(
                f"/v1/account/clients/{cid}/resend-report",
                headers={"Authorization": auth},
            )

        assert any(
            "resend" in r.getMessage().lower() or "email" in r.getMessage().lower()
            for r in caplog.records
        )

    def test_wrong_tenant_returns_404(self, client):
        """Client belonging to another tenant returns 404."""
        _, cid, _ = _make_setup()
        _, _, other_auth = _make_setup()
        resp = client.post(
            f"/v1/account/clients/{cid}/resend-report",
            headers={"Authorization": other_auth},
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client):
        _, cid, _ = _make_setup()
        resp = client.post(f"/v1/account/clients/{cid}/resend-report")
        assert resp.status_code == 401
