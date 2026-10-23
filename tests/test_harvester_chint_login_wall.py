"""Chint login walls: a CORRECT password must never read as a wrong one.

Chint's portal answers every sign-in with /api/users/user/doLogin JSON and can
park a correct password on a human-only step (EULA, account picker, texted code).
The harvester used to see only "the password box is still on screen", charge the
correct password to the lockout counter and pause the login as "re-enter your
password". These tests pin the fix: the verdict is read from Chint's own JSON, a
wall is recorded as `login_blocked` (no pause, clears an old streak), and the
scheduler re-checks it every BLOCKED_RETRY instead of every 3 minutes.
"""
from __future__ import annotations

import asyncio
import base64
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api import crypto
from api.db import SessionLocal
from api.harvester import credentials as cc
from api.harvester.scheduler import (
    BLOCKED_RETRY,
    MAX_LOGIN_FAILS,
    _is_due,
    accounts_with_recent_fresh_login,
    blocked_credentials,
)
from api.harvester.vendors.chint import ChintVendor, block_message, login_verdict
from api.models import HarvestRun, PortalLoginStatus, Tenant, now

TENANT = "ten_chintwall_t1"


@pytest.fixture(autouse=True)
def _armed_crypto():
    old = os.environ.get(crypto.ENV_KEY)
    os.environ[crypto.ENV_KEY] = base64.urlsafe_b64encode(b"c" * 32).decode()
    crypto._cache.clear()
    yield
    if old is None:
        os.environ.pop(crypto.ENV_KEY, None)
    else:
        os.environ[crypto.ENV_KEY] = old
    crypto._cache.clear()


def _cred(db, username: str):
    if db.get(Tenant, TENANT) is None:
        db.add(Tenant(id=TENANT, name=TENANT, contact_email=f"{TENANT}@example.com",
                      tenant_key=f"key_{TENANT}", active=True, is_demo=True))
        db.flush()
    row = cc.upsert_credential(db, TENANT, "chint", username, "pw", enable=True)
    db.flush()
    return row


# ── 1. Chint's own verdict ─────────────────────────────────────────────────

def test_verdict_reads_the_spa_branches():
    assert login_verdict({"success": True, "data": {"agreeResult": True}}) == {"kind": "ok"}
    assert login_verdict({"success": True, "data": {"agreeResult": False}}) == {"kind": "eula"}
    assert login_verdict({"success": True, "data": {}})["kind"] == "eula"
    assert login_verdict({"success": True, "data": {"checkOtp": True, "mobile": "x"}}) == {"kind": "otp"}
    assert login_verdict({"success": True, "data": {"checkUserList": [{}, {}]}}) == {
        "kind": "pick_account", "accounts": 2}
    rej = login_verdict({"success": False, "msg": "Incorrect account or password"})
    assert rej == {"kind": "rejected", "msg": "Incorrect account or password"}
    assert login_verdict("not json") is None


def test_only_human_walls_produce_an_instruction():
    eula = block_message({"kind": "eula"})
    assert eula and "user agreement" in eula and "monitor.chintpowersystems.com" in eula
    assert "texted" in block_message({"kind": "otp"})
    assert "3 different Chint accounts" in block_message({"kind": "pick_account", "accounts": 3})
    # A real rejection or a clean sign-in is NOT a wall.
    assert block_message({"kind": "rejected", "msg": "x"}) is None
    assert block_message({"kind": "ok"}) is None
    assert block_message(None) is None


class _Page:
    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn


def _respond(page, url, body):
    async def _json():
        return body
    asyncio.run(page.handlers["response"](SimpleNamespace(url=url, json=_json)))


def test_vendor_hooks_name_the_wall_from_the_login_response():
    v, page = ChintVendor(), _Page()
    asyncio.run(v.prepare_page(page))
    # Unrelated traffic is ignored.
    _respond(page, "https://monitor.chintpowersystems.com:8443/api/asset/site/retrieve", {"x": 1})
    assert asyncio.run(v.login_block_reason(page)) is None
    _respond(page, "https://monitor.chintpowersystems.com:8443/api/users/user/doLogin",
             {"success": True, "data": {"agreeResult": False}})
    assert "user agreement" in asyncio.run(v.login_block_reason(page))
    assert asyncio.run(v.login_failure_detail(page)) is None


def test_vendor_hooks_explain_a_real_rejection():
    v, page = ChintVendor(), _Page()
    asyncio.run(v.prepare_page(page))
    _respond(page, "https://monitor.chintpowersystems.com:8443/api/users/user/doLogin",
             {"success": False, "msg": "Account does not exist"})
    assert asyncio.run(v.login_block_reason(page)) is None
    assert asyncio.run(v.login_failure_detail(page)) == (
        "Chint rejected the sign-in: Account does not exist")


# ── 2. the lockout counter ─────────────────────────────────────────────────

def test_a_wall_after_an_accepted_password_clears_the_pause():
    """The live shape: an old password failed into the pause, then the owner saved
    Chint's new one, which Chint accepted but parked on its EULA. That accepted
    password ends the streak; it must not read as a 4th failure."""
    with SessionLocal() as db:
        c = _cred(db, "wall-owner@example.com")
        for _ in range(MAX_LOGIN_FAILS):
            cc.record_health(db, c, ok=False, status="login_failed", started_at=now(),
                             fresh_login=True)
            db.flush()
        assert c.harvest_fails >= MAX_LOGIN_FAILS
        roster = db.execute(select(PortalLoginStatus).where(
            PortalLoginStatus.tenant_id == TENANT,
            PortalLoginStatus.username_lc == "wall-owner@example.com")).scalar_one()
        assert roster.paused is True

        cc.record_health(db, c, ok=False, status="login_blocked", started_at=now(),
                         fresh_login=True, error=block_message({"kind": "eula"}))
        db.flush()
        assert c.harvest_fails == 0
        assert roster.paused is False and roster.fails == 0
        run = db.execute(select(HarvestRun).where(
            HarvestRun.username_lc == "wall-owner@example.com")
            .order_by(HarvestRun.id.desc()).limit(1)).scalar_one()
        assert run.status == "login_blocked" and "user agreement" in (run.detail or "")
        db.rollback()


# ── 3. the cadence ─────────────────────────────────────────────────────────

def _fake(fails, age_min):
    return SimpleNamespace(harvest_fails=fails, last_harvest_ok=False, provider="chint",
                           last_harvest_at=now() - timedelta(minutes=age_min))


def test_blocked_login_rechecks_on_the_gentle_cadence():
    mins = int(BLOCKED_RETRY.total_seconds() // 60)
    assert not _is_due(_fake(0, 10), now(), blocked=True)          # not every 3 min
    assert _is_due(_fake(0, mins + 1), now(), blocked=True)        # but never gives up
    # Even with an old streak on a sibling copy, a wall waits only BLOCKED_RETRY.
    assert _is_due(_fake(MAX_LOGIN_FAILS, mins + 1), now(), MAX_LOGIN_FAILS, blocked=True)


def test_blocked_credentials_follow_the_latest_run_only():
    with SessionLocal() as db:
        a = _cred(db, "blocked-now@example.com")
        b = _cred(db, "blocked-then-ok@example.com")
        t0 = now() - timedelta(minutes=20)
        db.add(HarvestRun(tenant_id=TENANT, provider="chint", username_lc=a.username_lc,
                          started_at=t0, status="login_blocked", logged_in_fresh=True))
        db.add(HarvestRun(tenant_id=TENANT, provider="chint", username_lc=b.username_lc,
                          started_at=t0, status="login_blocked", logged_in_fresh=True))
        db.add(HarvestRun(tenant_id=TENANT, provider="chint", username_lc=b.username_lc,
                          started_at=now(), status="ok", logged_in_fresh=True))
        db.flush()
        blocked = blocked_credentials(db)
        assert (TENANT, "chint", "blocked-now@example.com") in blocked
        assert (TENANT, "chint", "blocked-then-ok@example.com") not in blocked
        # An accepted password (even behind a wall) vouches for the account.
        assert ("chint", "blocked-now@example.com") in accounts_with_recent_fresh_login(db)
        db.rollback()
