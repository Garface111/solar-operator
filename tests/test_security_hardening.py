"""Security hardening (Jul 2026): utility-session encryption + session epoch.

Covers the residual dump risk (utility JWTs) and password-change logout-all.
"""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from api import crypto
from api.models import Base, Tenant, UtilitySession, now


KEY_A = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _reset_crypto():
    old = os.environ.get(crypto.ENV_KEY)
    os.environ.pop(crypto.ENV_KEY, None)
    crypto._cache.clear()
    crypto._warned = False
    yield
    if old is None:
        os.environ.pop(crypto.ENV_KEY, None)
    else:
        os.environ[crypto.ENV_KEY] = old
    crypto._cache.clear()
    crypto._warned = False


def set_key(*keys: str) -> None:
    os.environ[crypto.ENV_KEY] = ",".join(keys)
    crypto._cache.clear()


def clear_key() -> None:
    os.environ.pop(crypto.ENV_KEY, None)
    crypto._cache.clear()


@pytest.fixture()
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sec.db'}", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


def _seed_tenant(db) -> str:
    t = Tenant(
        id="ten_sec", name="Sec Co", contact_email="s@x.com",
        tenant_key="sol_live_sec_test_key_xxxx",
    )
    db.add(t)
    db.flush()
    return t.id


def test_utility_session_encrypted_at_rest(Session):
    set_key(KEY_A)
    with Session() as db:
        tid = _seed_tenant(db)
        s = UtilitySession(
            tenant_id=tid, provider="gmp",
            api_token="jwt-SECRET-should-not-appear-raw",
            refresh_token="refresh-SECRET-xyz",
            raw_payload={"apiToken": "jwt-SECRET-should-not-appear-raw", "x": 1},
            captured_at=now(),
        )
        db.add(s)
        db.commit()
        sid = s.id

    with Session() as db:
        raw_tok = db.execute(
            text("SELECT api_token FROM utility_sessions WHERE id = :i"), {"i": sid}
        ).scalar_one()
        assert crypto.is_encrypted(raw_tok), "api_token must be SOENC1 ciphertext"
        assert "jwt-SECRET" not in raw_tok
        raw_ref = db.execute(
            text("SELECT refresh_token FROM utility_sessions WHERE id = :i"), {"i": sid}
        ).scalar_one()
        assert crypto.is_encrypted(raw_ref)
        assert "refresh-SECRET" not in raw_ref
        raw_payload = db.execute(
            text("SELECT raw_payload FROM utility_sessions WHERE id = :i"), {"i": sid}
        ).scalar_one()
        assert crypto.is_encrypted(raw_payload)
        # ORM decrypt path still works transparently
        s = db.get(UtilitySession, sid)
        assert s.api_token == "jwt-SECRET-should-not-appear-raw"
        assert s.refresh_token == "refresh-SECRET-xyz"
        assert s.raw_payload["apiToken"] == "jwt-SECRET-should-not-appear-raw"


def test_utility_session_passthrough_without_key(Session):
    clear_key()
    with Session() as db:
        tid = _seed_tenant(db)
        s = UtilitySession(
            tenant_id=tid, provider="gmp",
            api_token="plain-jwt-abc",
            captured_at=now(),
        )
        db.add(s)
        db.commit()
        sid = s.id
    with Session() as db:
        raw = db.execute(
            text("SELECT api_token FROM utility_sessions WHERE id = :i"), {"i": sid}
        ).scalar_one()
        assert not crypto.is_encrypted(raw)
        assert "plain-jwt-abc" in raw


def test_session_epoch_invalidates_old_tokens(Session, monkeypatch):
    """Password-change epoch bump must reject tokens minted under the old epoch."""
    # Point account module at our isolated DB.
    from api import account as acct
    from api import db as dbmod

    engine = Session.kw["bind"] if hasattr(Session, "kw") else None
    # sessionmaker stores bind on .kw in SA 2
    bind = Session.kw.get("bind") if hasattr(Session, "kw") else None
    if bind is None:
        # rebuild: create dedicated engine for this test
        pass

    # Use the Session fixture's engine via monkeypatch of SessionLocal
    Sess = Session

    def _SL():
        return Sess()

    monkeypatch.setattr(acct, "SessionLocal", _SL)
    monkeypatch.setattr(dbmod, "SessionLocal", _SL)
    # Ensure signing secret stable
    monkeypatch.setattr(acct, "SESSION_SECRET", "test-session-secret-for-epoch")

    with Sess() as db:
        t = Tenant(
            id="ten_ep", name="Ep Co", contact_email="e@x.com",
            tenant_key="sol_live_epoch_test", session_epoch=0,
        )
        db.add(t)
        db.commit()

    tok0 = acct._sign_session("ten_ep", session_epoch=0)
    assert acct.tenant_from_session(f"Bearer {tok0}").id == "ten_ep"

    with Sess() as db:
        t = db.get(Tenant, "ten_ep")
        acct.bump_session_epoch(db, t)
        db.commit()

    with pytest.raises(Exception) as ei:
        acct.tenant_from_session(f"Bearer {tok0}")
    assert getattr(ei.value, "status_code", None) == 401

    tok1 = acct._sign_session("ten_ep", session_epoch=1)
    assert acct.tenant_from_session(f"Bearer {tok1}").id == "ten_ep"


def test_encrypt_script_targets_include_utility_sessions():
    from scripts import encrypt_vendor_credentials as ev
    cols = {(t, c) for t, _, c in ev._TARGETS}
    assert ("utility_sessions", "api_token") in cols
    assert ("utility_sessions", "refresh_token") in cols
    assert ("portal_credentials", "secret_enc") in cols
