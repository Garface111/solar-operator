"""Repeated fresh-login failures clear the persisted session jar.

The engine used to RE-PERSIST the Playwright storage_state on every
login_failed. A poisoned jar — a half-dead SSO cookie the portal will neither
honor nor replace with a login form — therefore re-pickled itself forever:
every run reused the same broken state, failed the same way, and saved it
again (the Fronius "no-form, not authenticated" loop, prod 2026-08).

Rule now: the FIRST fresh-login failure still persists state (it may hold a
consent cookie or partial IdP state worth one more try); the SECOND consecutive
one clears the jar so the next attempt starts clean. An ok run persists
normally and resets the counter, so a healthy credential never loses its warm
session.
"""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import select

from api import crypto
from api.db import SessionLocal
from api.harvester import credentials as cc
from api.harvester.engine import BrowserFarm
from api.models import PortalCredential, Tenant, now

TENANT = "ten_jar_reset_t1"
STATE = {"cookies": [{"name": "warm", "value": "x"}], "origins": []}


@pytest.fixture(autouse=True)
def _clean_rows():
    """The suite shares one DB — leave nothing behind (tenant, credentials,
    audit rows) or another file inherits a mystery failure."""
    yield
    from api.models import HarvestRun, InverterAlertState, PortalLoginStatus
    with SessionLocal() as db:
        for model in (HarvestRun, PortalCredential, PortalLoginStatus,
                      InverterAlertState):
            for row in db.query(model).filter_by(tenant_id=TENANT).all():
                db.delete(row)
        t = db.get(Tenant, TENANT)
        if t is not None:
            db.delete(t)
        db.commit()


@pytest.fixture(autouse=True)
def _armed_crypto():
    old = os.environ.get(crypto.ENV_KEY)
    os.environ[crypto.ENV_KEY] = base64.urlsafe_b64encode(b"k" * 32).decode()
    crypto._cache.clear()
    yield
    if old is None:
        os.environ.pop(crypto.ENV_KEY, None)
    else:
        os.environ[crypto.ENV_KEY] = old
    crypto._cache.clear()


def _cred(db, username="jar@example.com") -> PortalCredential:
    if db.get(Tenant, TENANT) is None:
        db.add(Tenant(id=TENANT, name=TENANT, contact_email=f"{TENANT}@example.com",
                      tenant_key=f"key_{TENANT}", active=True, is_demo=True))
        db.flush()
    row = cc.upsert_credential(db, TENANT, "fronius", username, "pw", enable=True)
    db.flush()
    row.harvest_fails = 0
    row.session_state_enc = STATE
    row.session_state_at = now()
    db.commit()
    return row


def _persist_failure(username):
    BrowserFarm._persist(
        TENANT, "fronius", username,
        storage_state=STATE, ok=False, status="login_failed",
        started_at=now(), fresh=True, rows=0,
        error="login outcome=no-form, not authenticated", shot=None,
        login_failed_fresh=True)


def _fetch(db, username):
    return db.execute(
        select(PortalCredential).where(
            PortalCredential.tenant_id == TENANT,
            PortalCredential.provider == "fronius",
            PortalCredential.username_lc == username,
        )
    ).scalar_one()


def test_first_failure_keeps_state_second_failure_clears_the_jar():
    u = "jar@example.com"
    with SessionLocal() as db:
        _cred(db, u)

    _persist_failure(u)                      # failure #1 — state kept
    with SessionLocal() as db:
        c = _fetch(db, u)
        assert c.harvest_fails == 1
        assert c.session_state_enc is not None, (
            "the first failure may keep the jar — it can hold consent/IdP "
            "state worth one more try"
        )

    _persist_failure(u)                      # failure #2 — jar cleared
    with SessionLocal() as db:
        c = _fetch(db, u)
        assert c.harvest_fails == 2
        assert c.session_state_enc is None, (
            "a second consecutive fresh-login failure must clear the persisted "
            "session state — re-pickling a poisoned jar is the eternal-failure loop"
        )
        assert c.session_state_at is None


def test_ok_run_still_persists_state_normally():
    u = "jar-ok@example.com"
    with SessionLocal() as db:
        _cred(db, u)
        # simulate a standing failure count — an OK run must not clear the jar
        _fetch(db, u).harvest_fails = 2
        db.commit()

    BrowserFarm._persist(
        TENANT, "fronius", u,
        storage_state=STATE, ok=True, status="ok",
        started_at=now(), fresh=True, rows=3, error=None, shot=None)
    with SessionLocal() as db:
        c = _fetch(db, u)
        assert c.harvest_fails == 0, "an ok fresh login resets the counter"
        assert c.session_state_enc is not None, "ok runs persist the warm session"
