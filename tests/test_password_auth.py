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
        """Post-fold, the nepool product's SPA lives at the folded home's
        /accounts proxy — the link must land there (NOT on AO's /login, and
        never on the sunsetting nepooloperator.com)."""
        cap = self._capture_link(monkeypatch, "nepool")
        assert "arrayoperator.com/accounts/?token=" in cap["html"]
        assert "/login?token=" not in cap["html"]
        assert "nepooloperator.com" not in cap["html"]


# ─── multi-tenant-per-email disambiguation (the login whack-a-mole fix) ─────────

class TestMultiTenantEmail:
    """One email can own a NEPOOL tenant AND an Array Operator tenant. Login must
    not fail or land in the wrong one."""

    def _two_accounts(self, email: str, nepool_pw: str, ao_pw: str):
        from api.account import _hash_password
        ids = {}
        with SessionLocal() as db:
            for product, pw in (("nepool", nepool_pw), ("array_operator", ao_pw)):
                tid = "ten_" + secrets.token_hex(6)
                db.add(Tenant(
                    id=tid, name=f"{product} Co", contact_email=email,
                    tenant_key="sol_live_" + secrets.token_urlsafe(12),
                    plan="standard", active=True, subscription_status="trialing",
                    product=product, password_hash=_hash_password(pw)))
                ids[product] = tid
            db.commit()
        return ids

    def test_correct_password_for_second_account_succeeds(self, client):
        """The AO password must work even though a NEPOOL tenant on the same email
        sorts first — the old code checked only one arbitrary tenant and 401'd."""
        email = f"dual_{secrets.token_hex(4)}@example.com"
        self._two_accounts(email, "NepoolPass1", "ArrayPass2")
        resp = client.post("/v1/auth/password-login",
                           json={"email": email, "password": "ArrayPass2"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

    def test_product_hint_routes_to_right_tenant(self, client):
        """When passwords match and product is given, the chosen tenant's product
        comes back as requested."""
        email = f"dual_{secrets.token_hex(4)}@example.com"
        ids = self._two_accounts(email, "SharedPass1", "SharedPass1")  # same pw both
        resp = client.post("/v1/auth/password-login",
                           json={"email": email, "password": "SharedPass1",
                                 "product": "array_operator"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["product"] == "array_operator"
        resp2 = client.post("/v1/auth/password-login",
                            json={"email": email, "password": "SharedPass1",
                                  "product": "nepool"})
        assert resp2.json()["product"] == "nepool"

    def test_wrong_password_still_401(self, client):
        email = f"dual_{secrets.token_hex(4)}@example.com"
        self._two_accounts(email, "NepoolPass1", "ArrayPass2")
        resp = client.post("/v1/auth/password-login",
                           json={"email": email, "password": "totallywrong9"})
        assert resp.status_code == 401


# ─── magic-link STRICT product scoping (the cross-product leak fix) ──────────────

class TestMagicLinkProductScoping:
    """issue_magic_link(product=...) must resolve ONLY within that product.

    The reported bug: a NEPOOL operator requested a sign-in link and got emailed
    a link into Array Operator, because the request was product-blind and the
    cross-product fallback picked whichever tenant was active/newest. With the
    SPA now passing product="nepool" (and AO passing "array_operator"), the
    backend must route each login to its OWN product's tenant — and must NOT
    fall back to the other product when no tenant matches.
    """

    def _two_accounts(self, email: str):
        """Make a NEPOOL + an Array Operator tenant on the SAME email. The AO one
        is created LAST so it sorts first under the (active, created_at desc)
        order the old product-blind path used — i.e. the worst case where a bare
        NEPOOL request would have leaked into AO."""
        ids = {}
        with SessionLocal() as db:
            for product in ("nepool", "array_operator"):
                tid = "ten_" + secrets.token_hex(6)
                db.add(Tenant(
                    id=tid, name=f"{product} Co", contact_email=email,
                    operator_name=f"{product} Owner",
                    tenant_key="sol_live_" + secrets.token_urlsafe(12),
                    plan="standard", active=True, subscription_status="trialing",
                    product=product))
                ids[product] = tid
            db.commit()
        return ids

    def _capture(self, monkeypatch):
        import api.account as account
        captured = {}
        def _fake(to, subject, html, text=None, **kw):
            captured["to"] = to
            captured["html"] = html
            captured["text"] = text
            captured["product"] = kw.get("product")
            return True
        monkeypatch.setattr(account, "_send_via_resend", _fake)
        return account, captured

    def test_nepool_request_with_dual_email_targets_nepool(self, monkeypatch):
        """A NEPOOL login on a shared email lands on the NEPOOL surface even
        though an Array Operator tenant exists (and sorts first). Post-fold both
        products live on arrayoperator.com — product scoping now shows in the
        PATH: nepool → the /accounts SPA, AO → /login."""
        account, cap = self._capture(monkeypatch)
        email = f"dual_{secrets.token_hex(4)}@example.com"
        self._two_accounts(email)
        sent = account.issue_magic_link(email, product="nepool")
        assert sent is True
        assert "arrayoperator.com/accounts/?token=" in cap["html"]
        assert "/login?token=" not in cap["html"]

    def test_array_operator_request_with_dual_email_targets_ao(self, monkeypatch):
        account, cap = self._capture(monkeypatch)
        email = f"dual_{secrets.token_hex(4)}@example.com"
        self._two_accounts(email)
        sent = account.issue_magic_link(email, product="array_operator")
        assert sent is True
        assert "arrayoperator.com/login?token=" in cap["html"]
        assert "nepooloperator.com" not in cap["html"]

    def test_no_fallback_when_product_absent(self, monkeypatch):
        """If the requested product has NO tenant for this email, refuse —
        never email a link into the other product's account."""
        account, cap = self._capture(monkeypatch)
        email = f"nepoolonly_{secrets.token_hex(4)}@example.com"
        # Only an Array Operator tenant exists on this email.
        with SessionLocal() as db:
            tid = "ten_" + secrets.token_hex(6)
            db.add(Tenant(
                id=tid, name="AO only", contact_email=email,
                operator_name="AO Owner",
                tenant_key="sol_live_" + secrets.token_urlsafe(12),
                plan="standard", active=True, subscription_status="trialing",
                product="array_operator"))
            db.commit()
        # A NEPOOL login for this email must NOT leak the AO account.
        sent = account.issue_magic_link(email, product="nepool")
        assert sent is False
        assert cap == {}  # nothing emailed

    def test_product_blind_request_keeps_legacy_fallback(self, monkeypatch):
        """A product-less caller (legacy/unknown origin) still resolves to the
        active/newest tenant so no existing integration breaks."""
        account, cap = self._capture(monkeypatch)
        email = f"legacy_{secrets.token_hex(4)}@example.com"
        with SessionLocal() as db:
            tid = "ten_" + secrets.token_hex(6)
            db.add(Tenant(
                id=tid, name="Legacy Co", contact_email=email,
                operator_name="Legacy Owner",
                tenant_key="sol_live_" + secrets.token_urlsafe(12),
                plan="standard", active=True, subscription_status="trialing",
                product="nepool"))
            db.commit()
        sent = account.issue_magic_link(email)  # no product
        assert sent is True
        assert "arrayoperator.com/accounts/?token=" in cap["html"]

