"""
Tests for password-based authentication.

Covers: set-password (valid / invalid), password-login (success / wrong /
unknown / no-password), change-password (with and without current-password
check), session token shape, and has_password flag on /v1/account.
"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select

from api.account import _verify_session, mint_session_for_tenant
from api.db import SessionLocal
from api.models import Tenant


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_tenant() -> tuple[str, str]:
    """Create a minimal active tenant; return (tenant_id, bearer_header)."""
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="PW Test Co",
            contact_email=f"{tid}@example.com",
            tenant_key="sol_live_" + secrets.token_urlsafe(16),
            plan="standard",
            active=True,
            subscription_status="active",
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _tenant_email(tid: str) -> str:
    with SessionLocal() as db:
        t = db.execute(
            select(Tenant).where(Tenant.id == tid)
        ).scalars().first()
        return t.contact_email


# ─── set-password ─────────────────────────────────────────────────────────────

class TestSetPassword:
    def test_set_valid_password(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "SecurePass1"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["has_password"] is True

    def test_too_short_rejected(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "Short1"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_no_letter_rejected(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "1234567890"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_no_digit_rejected(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "longenoughword"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_requires_session(self, client):
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "SecurePass1"},
        )
        assert resp.status_code == 401


# ─── password-login ───────────────────────────────────────────────────────────

class TestPasswordLogin:
    def test_login_success(self, client):
        tid, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "TestPassword1"},
            headers={"Authorization": auth},
        )
        email = _tenant_email(tid)
        resp = client.post(
            "/v1/auth/password-login",
            json={"email": email, "password": "TestPassword1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "session_token" in body
        assert isinstance(body["session_token"], str)
        assert len(body["session_token"]) > 20
        # product lets a per-product login route a wrong-brand account home.
        assert body.get("product") == "nepool"  # default product

    def test_login_returns_array_operator_product(self, client):
        tid, auth = _make_tenant()
        with SessionLocal() as db:
            db.get(Tenant, tid).product = "array_operator"
            db.commit()
        client.post(
            "/v1/auth/set-password",
            json={"password": "TestPassword1"},
            headers={"Authorization": auth},
        )
        resp = client.post(
            "/v1/auth/password-login",
            json={"email": _tenant_email(tid), "password": "TestPassword1"},
        )
        assert resp.status_code == 200
        assert resp.json()["product"] == "array_operator"

    def test_wrong_password_returns_401(self, client):
        tid, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "TestPassword1"},
            headers={"Authorization": auth},
        )
        email = _tenant_email(tid)
        resp = client.post(
            "/v1/auth/password-login",
            json={"email": email, "password": "WrongPass999"},
        )
        assert resp.status_code == 401
        assert "Invalid" in resp.json()["detail"]

    def test_unknown_email_returns_401(self, client):
        resp = client.post(
            "/v1/auth/password-login",
            json={"email": "nobody@nowhere-xyzzy.example", "password": "TestPass1234"},
        )
        assert resp.status_code == 401

    def test_no_password_set_returns_401(self, client):
        # Tenant exists but has never set a password
        tid, _ = _make_tenant()
        email = _tenant_email(tid)
        resp = client.post(
            "/v1/auth/password-login",
            json={"email": email, "password": "AnyPassword1"},
        )
        assert resp.status_code == 401

    def test_session_token_shape_matches_magic_link(self, client):
        """Session minted by password-login verifies with the same _verify_session
        used by the magic-link path — they are structurally identical."""
        tid, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "TestPassword1"},
            headers={"Authorization": auth},
        )
        email = _tenant_email(tid)
        resp = client.post(
            "/v1/auth/password-login",
            json={"email": email, "password": "TestPassword1"},
        )
        assert resp.status_code == 200
        token = resp.json()["session_token"]
        # _verify_session must recognise the token and return the correct tenant_id
        tenant_id = _verify_session(token)
        assert tenant_id == tid


# ─── change-password ──────────────────────────────────────────────────────────

class TestChangePassword:
    def test_change_with_correct_current(self, client):
        tid, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "InitialPass1"},
            headers={"Authorization": auth},
        )
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "NewPassword2x", "current_password": "InitialPass1"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        # Confirm new password works for login
        email = _tenant_email(tid)
        login = client.post(
            "/v1/auth/password-login",
            json={"email": email, "password": "NewPassword2x"},
        )
        assert login.status_code == 200

    def test_change_with_wrong_current_rejected(self, client):
        _, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "InitialPass1"},
            headers={"Authorization": auth},
        )
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "NewPassword2x", "current_password": "WrongCurrent1"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_change_without_current_when_required(self, client):
        _, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "InitialPass1"},
            headers={"Authorization": auth},
        )
        # Omit current_password — should fail since a password is already set
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "NewPassword2x"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400

    def test_first_set_no_current_required(self, client):
        """First-time set doesn't require current_password."""
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/auth/set-password",
            json={"password": "FirstPassword1"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200


# ─── has_password on /v1/account ─────────────────────────────────────────────

class TestHasPasswordFlag:
    def test_false_before_set(self, client):
        _, auth = _make_tenant()
        resp = client.get("/v1/account", headers={"Authorization": auth})
        assert resp.status_code == 200
        assert resp.json()["has_password"] is False

    def test_true_after_set(self, client):
        _, auth = _make_tenant()
        client.post(
            "/v1/auth/set-password",
            json={"password": "TestPassword1"},
            headers={"Authorization": auth},
        )
        resp = client.get("/v1/account", headers={"Authorization": auth})
        assert resp.status_code == 200
        assert resp.json()["has_password"] is True


# ── magic-link email lands on the correct PRODUCT site ────────────────────────

class TestMagicLinkTarget:
    def _capture_link(self, monkeypatch, product):
        import api.account as account
        captured = {}
        def _fake(to, subject, html, text=None, **kw):
            captured["html"] = html
            captured["text"] = text
            return True
        monkeypatch.setattr(account, "_send_via_resend", _fake)
        tid = "ten_" + secrets.token_hex(6)
        with SessionLocal() as db:
            db.add(Tenant(
                id=tid, name="ML Co", contact_email=f"{tid}@example.com",
                operator_name="ML Owner", tenant_key="sol_live_" + secrets.token_urlsafe(12),
                plan="standard", active=True, subscription_status="trialing",
                product=product))
            db.commit()
            email = f"{tid}@example.com"
        account.issue_magic_link(email)
        return captured

    def test_array_operator_magic_link_targets_arrayoperator_login(self, monkeypatch):
        cap = self._capture_link(monkeypatch, "array_operator")
        assert "arrayoperator.com/login?token=" in cap["html"]
        assert "arrayoperator.com/login?token=" in cap["text"]
        assert "nepooloperator.com" not in cap["html"]

    def test_nepool_magic_link_targets_nepool_accounts(self, monkeypatch):
        cap = self._capture_link(monkeypatch, "nepool")
        assert "nepooloperator.com/accounts/?token=" in cap["html"]
        assert "arrayoperator.com" not in cap["html"]
