"""has_active_credential distinguishes "a password is on file" from
Tenant.capture_mode == 'cloud', which is a preference an owner can carry (or
that defaults on) without ever actually saving one — three real production
tenants were found in exactly that state: capture_mode='cloud', zero
PortalCredential rows for gmp. That gap is what used to make the reauth emails
send an instruction ("the extension will capture a fresh session") that could
not possibly fix their account, since there was nothing to retry with.
"""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import PortalCredential, Tenant
from api.harvester.credentials import has_active_credential


def _mk_tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Credential Gate Test", contact_email=f"{tid}@t.t",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
            capture_mode="cloud",
        ))
        db.commit()
    return tid


def test_no_row_at_all_is_not_an_active_credential():
    tid = _mk_tenant()
    with SessionLocal() as db:
        assert has_active_credential(db, tid, "gmp") is False


def test_a_saved_enabled_password_counts():
    tid = _mk_tenant()
    with SessionLocal() as db:
        db.add(PortalCredential(
            tenant_id=tid, provider="gmp", username="o@ex.com", username_lc="o@ex.com",
            secret_enc="enc-test", cloud_capture_enabled=True,
        ))
        db.commit()
        assert has_active_credential(db, tid, "gmp") is True


def test_a_disabled_credential_does_not_count():
    """Toggled off in the UI — the harvester will not use it, so it must read
    the same as having none: there is nothing that will self-heal."""
    tid = _mk_tenant()
    with SessionLocal() as db:
        db.add(PortalCredential(
            tenant_id=tid, provider="gmp", username="o@ex.com", username_lc="o@ex.com",
            secret_enc="enc-test", cloud_capture_enabled=False,
        ))
        db.commit()
        assert has_active_credential(db, tid, "gmp") is False


def test_a_row_with_no_secret_does_not_count():
    """A username saved without ever entering a password (e.g. a partial UI
    save) is not something the harvester can log in with."""
    tid = _mk_tenant()
    with SessionLocal() as db:
        db.add(PortalCredential(
            tenant_id=tid, provider="gmp", username="o@ex.com", username_lc="o@ex.com",
            secret_enc=None, cloud_capture_enabled=True,
        ))
        db.commit()
        assert has_active_credential(db, tid, "gmp") is False


def test_a_credential_for_a_different_provider_does_not_count():
    """A saved SMA login says nothing about whether GMP has one."""
    tid = _mk_tenant()
    with SessionLocal() as db:
        db.add(PortalCredential(
            tenant_id=tid, provider="sma", username="o@ex.com", username_lc="o@ex.com",
            secret_enc="enc-test", cloud_capture_enabled=True,
        ))
        db.commit()
        assert has_active_credential(db, tid, "gmp") is False
        assert has_active_credential(db, tid, "sma") is True
