"""Vault hardening (Jul 2026): churn gate, no fleet-decrypt schedule, desk tenant auth."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from api import crypto
from api.models import Base, PortalCredential, Tenant, now


KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _reset_crypto():
    old = os.environ.get(crypto.ENV_KEY)
    old_vd = os.environ.get("SO_VAULT_DECRYPT")
    os.environ.pop(crypto.ENV_KEY, None)
    os.environ.pop("SO_VAULT_DECRYPT", None)
    crypto._cache.clear()
    crypto._warned = False
    yield
    if old is None:
        os.environ.pop(crypto.ENV_KEY, None)
    else:
        os.environ[crypto.ENV_KEY] = old
    if old_vd is None:
        os.environ.pop("SO_VAULT_DECRYPT", None)
    else:
        os.environ["SO_VAULT_DECRYPT"] = old_vd
    crypto._cache.clear()
    crypto._warned = False


@pytest.fixture()
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'vault.db'}", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


def _tenant(db, tid="ten_a", active=True, email="a@x.com"):
    t = Tenant(
        id=tid, name="A", contact_email=email,
        tenant_key=f"sol_live_{tid}", active=active,
    )
    db.add(t)
    db.flush()
    return t


def test_inactive_tenant_not_allowed_even_when_real_customers():
    from api.harvester.scheduler import _tenant_allowed

    db = MagicMock()
    t = MagicMock()
    t.active = False
    t.is_demo = False
    db.get.return_value = t
    assert _tenant_allowed(db, "ten_x", allow_real=True, allowlist=set()) is False


def test_active_real_customer_allowed():
    from api.harvester.scheduler import _tenant_allowed

    db = MagicMock()
    t = MagicMock()
    t.active = True
    t.is_demo = False
    db.get.return_value = t
    assert _tenant_allowed(db, "ten_x", allow_real=True, allowlist=set()) is True


def test_teardown_hard_deletes_portal_credentials(Session):
    os.environ[crypto.ENV_KEY] = KEY
    crypto._cache.clear()
    from api.vault_lifecycle import teardown_cloud_capture_for_tenant

    with Session() as db:
        _tenant(db)
        db.add(PortalCredential(
            tenant_id="ten_a", provider="gmp", username="u", username_lc="u",
            secret_enc="hunter2", cloud_capture_enabled=True,
        ))
        db.commit()
        n = db.execute(select(PortalCredential)).scalars().all()
        assert len(n) == 1
        res = teardown_cloud_capture_for_tenant(db, "ten_a", reason="test")
        db.commit()
        assert res["ok"] is True
        assert res["counts"]["portal_credential"] == 1
        left = db.execute(select(PortalCredential)).scalars().all()
        assert left == []


def test_vault_decrypt_disabled_on_web(Session):
    os.environ[crypto.ENV_KEY] = KEY
    os.environ["SO_VAULT_DECRYPT"] = "1"
    crypto._cache.clear()
    with Session() as db:
        _tenant(db)
        row = PortalCredential(
            tenant_id="ten_a", provider="gmp", username="u", username_lc="u",
            secret_enc="s3cret-password", cloud_capture_enabled=True,
        )
        db.add(row)
        db.commit()
        rid = row.id
        raw = db.execute(
            text("SELECT secret_enc FROM portal_credential WHERE id=:i"), {"i": rid}
        ).scalar_one()
        assert crypto.is_encrypted(raw)

    # Simulate public web: decrypt off
    os.environ["SO_VAULT_DECRYPT"] = "0"
    crypto._cache.clear()
    with Session() as db:
        with pytest.raises(RuntimeError, match="Vault decrypt is disabled"):
            _ = db.get(PortalCredential, rid).secret_enc


def test_cloud_capture_status_never_decrypts_vault_on_web(Session, monkeypatch):
    """The /v1/cloud-capture/status panel must load without unwrapping the vault.

    Regression: the endpoint selected the full PortalCredential ORM entity, so
    fetching each row ran the EncryptedVaultStr/JSON decryptors at fetch time —
    which the public web process refuses (SO_VAULT_DECRYPT=0), 500ing the panel.
    """
    from api import cloud_capture

    os.environ[crypto.ENV_KEY] = KEY
    os.environ["SO_VAULT_DECRYPT"] = "1"  # arm decrypt to store an encrypted row
    crypto._cache.clear()
    with Session() as db:
        t = _tenant(db)
        db.add(PortalCredential(
            tenant_id=t.id, provider="gmp", username="u", username_lc="u",
            secret_enc="s3cret-password", cloud_capture_enabled=True,
            session_state_enc={"cookies": ["c"]},
        ))
        db.commit()

    # Simulate public web: decrypt is refused for vault secrets.
    os.environ["SO_VAULT_DECRYPT"] = "0"
    crypto._cache.clear()

    class _T:
        id = "ten_a"

    monkeypatch.setattr(cloud_capture, "SessionLocal", Session)
    monkeypatch.setattr(cloud_capture, "tenant_from_session", lambda a: _T())

    out = cloud_capture.status(authorization="Bearer x")  # must not raise
    assert len(out["credentials"]) == 1
    row = out["credentials"][0]
    assert row["provider"] == "gmp"
    assert row["has_session"] is True  # derived at SQL layer, secret never decrypted
    assert row["sign_ins_this_month"] == 0
    assert row["attempts_this_month"] == 0
    assert "activity_month" in out


def test_list_all_meta_requires_tenant_id(Session):
    from api.harvester.credentials import list_all_meta

    with Session() as db:
        _tenant(db)
        with pytest.raises(ValueError, match="tenant_id"):
            list_all_meta(db)


def test_desk_auth_binds_tenant_id(monkeypatch):
    from api import energy_agent_sovereign_desk as desk
    from fastapi import HTTPException

    class T:
        id = "ten_stranger"
        contact_email = "ford.genereaux@gmail.com"  # email alone must not unlock

    monkeypatch.setattr(desk, "tenant_from_session", lambda a: T())
    monkeypatch.setattr(desk, "require_not_demo", lambda t: None)
    with pytest.raises(HTTPException) as ei:
        desk._auth_ford("Bearer x")
    assert ei.value.status_code == 403

    class T2:
        id = "ten_aaad29f08dbe9943"
        contact_email = "other@example.com"

    monkeypatch.setattr(desk, "tenant_from_session", lambda a: T2())
    t, email = desk._auth_ford("Bearer x")
    assert t.id == "ten_aaad29f08dbe9943"


def test_creds_repr_hides_password():
    from api.harvester.credentials import Creds

    c = Creds(
        tenant_id="t", provider="gmp", username="u",
        password="SUPERSECRET", login_host=None, session_state={"k": 1},
    )
    r = repr(c)
    assert "SUPERSECRET" not in r
    assert "password" not in r or "password=" not in r


def test_run_scheduler_defaults_off(monkeypatch):
    from api import scheduler as sched

    monkeypatch.delenv("RUN_SCHEDULER", raising=False)
    assert sched.scheduler_enabled() is False
    monkeypatch.setenv("RUN_SCHEDULER", "1")
    assert sched.scheduler_enabled() is True


def test_sovereign_ops_defaults_fail_closed(monkeypatch):
    from api import energy_agent_sovereign_ops as ops

    monkeypatch.delenv("SOVEREIGN_OPS_AUTHORITY", raising=False)
    monkeypatch.delenv("SOVEREIGN_CREDENTIALS_UNLOCKED", raising=False)
    monkeypatch.delenv("SOVEREIGN_PORTAL_SIGN_OFF", raising=False)
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    assert ops.ops_enabled() is False
    assert ops.credentials_unlocked() is False
    assert ops.portal_signoff_enabled() is False


def test_decrypt_audit_includes_context(caplog):
    """Vault decrypt logs role + tenant/provider context, never the secret."""
    import logging
    from api import crypto

    os.environ[crypto.ENV_KEY] = KEY
    os.environ["SO_VAULT_DECRYPT"] = "1"
    os.environ["PROCESS_ROLE"] = "cloud-capture-harvester"
    crypto._cache.clear()

    ct = crypto.encrypt_str("hunter2-not-in-log")
    crypto.set_decrypt_audit_context(tenant_id="ten_x", provider="gmp", job_id="harvest")
    with caplog.at_level(logging.INFO, logger="solar.crypto"):
        plain = crypto.decrypt_vault_str(ct)
    crypto.clear_decrypt_audit_context()
    assert plain == "hunter2-not-in-log"
    joined = " ".join(r.message for r in caplog.records)
    assert "kind=vault" in joined
    assert "tenant_id=ten_x" in joined
    assert "provider=gmp" in joined
    assert "hunter2" not in joined


def test_rearm_all_requires_tenant_id(Session):
    from api.harvester.credentials import rearm_all

    with Session() as db:
        with pytest.raises(ValueError, match="tenant_id"):
            rearm_all(db)


def test_admin_vault_stats_requires_header(monkeypatch):
    from api import admin_vault
    from fastapi import HTTPException

    monkeypatch.setattr(admin_vault, "ADMIN_API_KEY", "test-admin-key-xyz")
    with pytest.raises(HTTPException) as ei:
        admin_vault._check(None, "test-admin-key-xyz")  # query key rejected
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei2:
        admin_vault._check("wrong", None)
    assert ei2.value.status_code == 403
    admin_vault._check("test-admin-key-xyz", None)  # ok


def test_prune_harvest_runs_keeps_recent(Session, monkeypatch):
    """Old harvest_run rows are hard-deleted; recent ones stay. Floor is 7 days."""
    from datetime import timedelta
    from api import vault_retention
    from api.models import HarvestRun

    monkeypatch.setattr(vault_retention, "SessionLocal", Session)
    with Session() as db:
        _tenant(db)
        db.add(HarvestRun(
            tenant_id="ten_a", provider="gmp", username_lc="u",
            status="ok", started_at=now() - timedelta(days=3),
        ))
        db.add(HarvestRun(
            tenant_id="ten_a", provider="gmp", username_lc="u",
            status="ok", started_at=now() - timedelta(days=60),
        ))
        db.commit()

    # keep_days=1 floors to 7 → 3-day-old survives, 60-day-old dies
    out = vault_retention.prune_harvest_runs(keep_days=1)
    assert out["ok"] is True
    assert out["keep_days"] == 7
    assert out["deleted"] == 1
    with Session() as db:
        left = db.execute(select(HarvestRun)).scalars().all()
        assert len(left) == 1
        assert (now() - left[0].started_at).days < 10


def test_sentry_scrubs_contexts():
    from api import observability
    event = {
        "request": {"url": "https://x/?token=secretvalue123"},
        "contexts": {"client": {"url": "https://x/?token=abc", "stack": "ok"}},
        "breadcrumbs": {"values": [{"message": "Bearer abcdef", "data": {"password": "x"}}]},
        "exception": {"values": [{"stacktrace": {"frames": [{"vars": {"body": "nope"}}]}}]},
    }
    out = observability._before_send(event, {})
    assert "secretvalue" not in str(out.get("request", {}))
    assert "token=abc" not in str(out.get("contexts", {}))
    crumb_msg = out["breadcrumbs"]["values"][0]["message"]
    assert "abcdef" not in crumb_msg or "redacted" in crumb_msg.lower()
    assert out["breadcrumbs"]["values"][0]["data"]["password"] == "[redacted]"
