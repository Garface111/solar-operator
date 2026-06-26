"""Encryption-at-rest for vendor credentials (api/crypto.py).

Proves the four guarantees that make this safe to ship:
  1. PASS-THROUGH when SO_CONFIG_KEY is unset — storage is byte-identical to the
     old plaintext columns, so deploying the code changes nothing.
  2. ENCRYPTED AT REST when the key is set — the raw DB value is ciphertext, the
     secret never appears in it, but the ORM reader sees the exact same dict.
  3. MIXED-MODE — a row written as plaintext (pre-key) still reads correctly once
     the key is switched on, so there is no flag day.
  4. ROTATABLE + FAILS LOUD — MultiFernet rotation works; an encrypted row with
     the key removed raises instead of silently returning garbage.

Also covers the legacy Array.solaredge_api_key column and the one-time migration
script (scripts/encrypt_vendor_credentials.py).
"""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from api import crypto
from api.models import Base, Tenant, Array, InverterConnection

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()

SE_CONFIG = {"api_key": "SE_SECRET_abc123", "site_id": 416160}


# ── env / key helpers ────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_crypto():
    """Snapshot/restore SO_CONFIG_KEY and clear the module's memoized state so
    each test controls the key independently."""
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


# ── isolated DB (own engine so the EncryptedJSON column is created as TEXT) ───
@pytest.fixture()
def db_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'enc.db'}", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def Session(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False, future=True)


def _seed_array(db) -> int:
    t = Tenant(id="ten_enc", name="Enc Co", contact_email="e@x.com", tenant_key="sol_live_enc")
    db.add(t)
    db.flush()
    a = Array(tenant_id=t.id, name="Test Array")
    db.add(a)
    db.flush()
    return a.id


def _raw_config(db, conn_id: int) -> str:
    """The value as physically stored — bypasses the ORM decrypt path."""
    return db.execute(
        text("SELECT config FROM inverter_connections WHERE id = :i"), {"i": conn_id}
    ).scalar_one()


# ── 1. pass-through (no key) ─────────────────────────────────────────────────
def test_passthrough_no_key_is_plaintext(Session):
    clear_key()
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        db.commit()
        cid = c.id

    with Session() as db:
        raw = _raw_config(db, cid)
        assert not crypto.is_encrypted(raw), "no key set → must store plaintext"
        assert "SE_SECRET_abc123" in raw, "plaintext JSON should contain the key verbatim"
        # reader sees the exact dict
        c = db.get(InverterConnection, cid)
        assert c.config == SE_CONFIG
        assert c.config["api_key"] == "SE_SECRET_abc123"
        assert (c.config or {}).get("site_id") == 416160


# ── 2. encrypted at rest (key set) ───────────────────────────────────────────
def test_encrypted_at_rest_with_key(Session):
    set_key(KEY_A)
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        db.commit()
        cid = c.id

    with Session() as db:
        raw = _raw_config(db, cid)
        assert crypto.is_encrypted(raw), "key set → must store ciphertext envelope"
        assert "SE_SECRET_abc123" not in raw, "secret must NOT appear at rest"
        assert "416160" not in raw
        # transparent decrypt on read
        c = db.get(InverterConnection, cid)
        assert c.config == SE_CONFIG
        assert c.config["api_key"] == "SE_SECRET_abc123"


# ── 3. mixed-mode: plaintext row stays readable after the key is switched on ──
def test_unmigrated_plaintext_readable_after_key_on(Session):
    clear_key()
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="fronius",
                               config={"access_token": "FR_tok", "system_id": "sys9"})
        db.add(c)
        db.commit()
        cid = c.id

    # operator provisions the key AFTER the row already exists (not yet migrated)
    set_key(KEY_A)
    with Session() as db:
        c = db.get(InverterConnection, cid)
        assert c.config == {"access_token": "FR_tok", "system_id": "sys9"}, \
            "un-migrated plaintext row must still decode once the key is on"
        # and a write now upgrades it to ciphertext
        c.config = {"access_token": "FR_tok2", "system_id": "sys9"}
        db.commit()
    with Session() as db:
        assert crypto.is_encrypted(_raw_config(db, cid))


# ── 4. rotation + fail-loud ──────────────────────────────────────────────────
def test_key_rotation_via_script(Session, db_engine):
    """Full rotation: read under [new, old], re-wrap under new, retire old."""
    from scripts import encrypt_vendor_credentials as script

    set_key(KEY_A)
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        db.commit()
        cid = c.id

    # introduce key B as primary, keep A for decryption — reads keep working
    set_key(KEY_B, KEY_A)
    with Session() as db:
        assert db.get(InverterConnection, cid).config == SE_CONFIG, \
            "old-key ciphertext must still decrypt under MultiFernet([B, A])"

    # re-wrap every row under the new primary so A can be retired
    rep = script.process(db_engine, mode="rotate", apply=True, out=lambda *_: None)
    assert rep["inverter_connections.config"]["changed"] == 1

    # retire A entirely: the row now reads under B alone …
    set_key(KEY_B)
    with Session() as db:
        assert db.get(InverterConnection, cid).config == SE_CONFIG

    # … and the OLD key can no longer read it — proving the re-wrap was real
    set_key(KEY_A)
    with Session() as db:
        with pytest.raises(InvalidToken):
            _ = db.get(InverterConnection, cid).config


def test_encrypted_but_no_key_fails_loud(Session):
    set_key(KEY_A)
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        db.commit()
        cid = c.id

    clear_key()  # key lost / unset while data is encrypted
    with Session() as db:
        with pytest.raises(RuntimeError, match="not set"):
            _ = db.get(InverterConnection, cid).config


def test_wrong_key_raises_invalid_token(Session):
    set_key(KEY_A)
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        db.commit()
        cid = c.id

    set_key(KEY_B)  # a different, wrong key (no overlap)
    with Session() as db:
        with pytest.raises(InvalidToken):
            _ = db.get(InverterConnection, cid).config


def test_malformed_key_raises(Session):
    set_key("not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="invalid"):
        crypto.encrypt_str("x")


# ── legacy Array.solaredge_api_key column ────────────────────────────────────
def test_legacy_array_api_key_encrypted_and_queryable(Session):
    set_key(KEY_A)
    with Session() as db:
        t = Tenant(id="ten_l", name="L", contact_email="l@x.com", tenant_key="sol_live_l")
        db.add(t)
        db.flush()
        a = Array(tenant_id=t.id, name="Legacy", solaredge_api_key="LEGACY_KEY_xyz",
                  solaredge_site_id=99)
        db.add(a)
        db.commit()
        aid = a.id

    with Session() as db:
        raw = db.execute(
            text("SELECT solaredge_api_key FROM arrays WHERE id = :i"), {"i": aid}
        ).scalar_one()
        assert crypto.is_encrypted(raw)
        assert "LEGACY_KEY_xyz" not in raw
        # transparent read
        a = db.get(Array, aid)
        assert a.solaredge_api_key == "LEGACY_KEY_xyz"
        assert a.solaredge_site_id == 99  # non-secret, plaintext
        # the SQL-level IS NOT NULL filter still finds it (ciphertext is non-null)
        found = db.execute(
            select(Array.id).where(Array.solaredge_api_key.is_not(None))
        ).scalars().all()
        assert aid in found


def test_legacy_passthrough_no_key(Session):
    clear_key()
    with Session() as db:
        t = Tenant(id="ten_l2", name="L2", contact_email="l2@x.com", tenant_key="sol_live_l2")
        db.add(t)
        db.flush()
        a = Array(tenant_id=t.id, name="Legacy2", solaredge_api_key="PLAIN_KEY")
        db.add(a)
        db.commit()
        aid = a.id
    with Session() as db:
        raw = db.execute(
            text("SELECT solaredge_api_key FROM arrays WHERE id = :i"), {"i": aid}
        ).scalar_one()
        assert raw == "PLAIN_KEY"  # byte-identical to old behavior


# ── one-time migration script ────────────────────────────────────────────────
def test_migration_script_encrypts_then_idempotent(Session, db_engine):
    from scripts import encrypt_vendor_credentials as script

    # seed plaintext rows (no key)
    clear_key()
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        a2 = Array(tenant_id="ten_enc", name="A2", solaredge_api_key="K2", solaredge_site_id=7)
        db.add(a2)
        db.commit()
        cid = c.id

    engine = db_engine

    # provision key, DRY-RUN first (writes nothing)
    set_key(KEY_A)
    rep = script.process(engine, apply=False, out=lambda *_: None)
    assert rep["inverter_connections.config"]["changed"] == 1
    assert rep["arrays.solaredge_api_key"]["changed"] == 1
    with Session() as db:
        assert not crypto.is_encrypted(_raw_config(db, cid)), "dry-run must not write"

    # APPLY
    script.process(engine, apply=True, out=lambda *_: None)
    with Session() as db:
        assert crypto.is_encrypted(_raw_config(db, cid))
        assert db.get(InverterConnection, cid).config == SE_CONFIG  # still reads right

    # idempotent: a second apply changes nothing (all rows already enveloped)
    rep2 = script.process(engine, apply=True, out=lambda *_: None)
    assert rep2["inverter_connections.config"]["changed"] == 0
    assert rep2["arrays.solaredge_api_key"]["changed"] == 0


def test_migration_script_decrypt_roundtrip(Session, db_engine):
    from scripts import encrypt_vendor_credentials as script

    set_key(KEY_A)
    with Session() as db:
        aid = _seed_array(db)
        c = InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG))
        db.add(c)
        db.commit()
        cid = c.id
    engine = db_engine
    with Session() as db:
        assert crypto.is_encrypted(_raw_config(db, cid))

    # rollback: decrypt every row back to plaintext (the pre-key-removal step)
    script.process(engine, mode="decrypt", apply=True, out=lambda *_: None)
    with Session() as db:
        raw = _raw_config(db, cid)
        assert not crypto.is_encrypted(raw)
        assert "SE_SECRET_abc123" in raw
        # and it's still a valid config the ORM round-trips
        assert db.get(InverterConnection, cid).config == SE_CONFIG


def test_verify_live_hands_decrypted_config_to_adapter(Session, db_engine, monkeypatch):
    """The --verify live check must decrypt before hitting the vendor API — i.e.
    the adapter receives the real secret, not the ciphertext envelope."""
    from scripts import encrypt_vendor_credentials as script
    from api import inverters

    set_key(KEY_A)
    with Session() as db:
        aid = _seed_array(db)
        db.add(InverterConnection(array_id=aid, vendor="solaredge", config=dict(SE_CONFIG)))
        db.commit()

    seen = {}

    def fake_fetch_live(vendor, config):
        seen["vendor"], seen["config"] = vendor, dict(config)
        return {"currentPower": 4200}

    monkeypatch.setattr(inverters, "fetch_live", fake_fetch_live)
    rep = script.verify_live(db_engine, out=lambda *_: None)
    assert rep == {"ok": 1, "fail": 0, "results": [(aid, "solaredge", True, None)]}
    assert seen["vendor"] == "solaredge"
    assert seen["config"] == SE_CONFIG, "live verify must pass the DECRYPTED creds to the adapter"
