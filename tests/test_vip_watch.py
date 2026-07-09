"""VIP watch — the tight (minutes, not days) self-heal staleness bar applies
to EVERY tenant (Ford, 2026-07-08: "all accounts should be babied like
this"); the Ford-alert half is tiered so it doesn't flood his inbox.

Pinned: (1) vip_stale_vendors only fires during DAYLIGHT (a normal overnight
gap must never count), and only past VIP_STALE_MINUTES, for ANY tenant; (2)
vip_watch_sweep alerts Ford ONCE per (tenant, array) incident and clears on
recovery, for EVERY active tenant — but a plain tenant only alerts past the
much wider ALERT_AFTER_MINUTES_DEFAULT bar, while vip_watch=True gets the
fast ALERT_AFTER_MINUTES_VIP one.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from api.db import SessionLocal
from api.models import Array, Inverter, InverterAlertState, Tenant
from api.vip_watch import (
    ALERT_AFTER_MINUTES_DEFAULT, ALERT_AFTER_MINUTES_VIP, VIP_STALE_MINUTES,
    vip_stale_vendors, vip_watch_sweep,
)


def _mk_tenant(*, vip_watch: bool = False) -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="VIP Watch Test", contact_email=f"{tid}@t.t",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
            vip_watch=vip_watch,
        ))
        db.commit()
    return tid


def _mk_array_with_inverter(tid: str, vendor: str, last_power_at) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Arr " + secrets.token_hex(3), fuel_type="solar")
        db.add(arr); db.flush()
        db.add(Inverter(
            tenant_id=tid, array_id=arr.id, vendor=vendor,
            serial="SN" + secrets.token_hex(4), last_power_at=last_power_at,
        ))
        db.commit()
        return arr.id


def test_vip_stale_vendors_ignores_fresh_capture(monkeypatch):
    import api.vip_watch as vw
    monkeypatch.setattr("api.inverter_fleet._daylight_for", lambda arr, default, _cache=None: True)
    tid = _mk_tenant()
    now_ = datetime.utcnow()
    _mk_array_with_inverter(tid, "sma", now_ - timedelta(minutes=2))
    with SessionLocal() as db:
        assert vip_stale_vendors(db, tid, now_=now_) == set()


def test_vip_stale_vendors_flags_past_the_minute_bar_in_daylight(monkeypatch):
    monkeypatch.setattr("api.inverter_fleet._daylight_for", lambda arr, default, _cache=None: True)
    tid = _mk_tenant()
    now_ = datetime.utcnow()
    _mk_array_with_inverter(tid, "sma", now_ - timedelta(minutes=VIP_STALE_MINUTES + 5))
    with SessionLocal() as db:
        assert vip_stale_vendors(db, tid, now_=now_) == {"sma"}


def test_vip_stale_vendors_never_fires_at_night(monkeypatch):
    """The whole point of the daylight gate: an overnight gap (no live power
    since sunset) must never read as staleness."""
    monkeypatch.setattr("api.inverter_fleet._daylight_for", lambda arr, default, _cache=None: False)
    tid = _mk_tenant()
    now_ = datetime.utcnow()
    _mk_array_with_inverter(tid, "fronius", now_ - timedelta(hours=10))
    with SessionLocal() as db:
        assert vip_stale_vendors(db, tid, now_=now_) == set()


def test_sweep_alerts_once_then_dedups_then_clears_on_recovery(monkeypatch):
    # vip_watch_sweep scans EVERY active tenant, and this suite's other tests
    # leave their own tenants sitting in the SAME database (no per-test
    # rollback) — some flagged, force-daylight'd True here, would ALSO read as
    # stale. Scope every assertion to THIS test's tenant so the suite's
    # shared-DB reality can't make this test flaky.
    sent = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: sent.append((subject, body)) or True)
    monkeypatch.setattr("api.inverter_fleet._daylight_for", lambda arr, default, _cache=None: True)
    tid = _mk_tenant(vip_watch=True)   # the fast tier this test pins
    now_ = datetime.utcnow()
    stale_at = now_ - timedelta(minutes=ALERT_AFTER_MINUTES_VIP + 10)
    array_id = _mk_array_with_inverter(tid, "sma", stale_at)

    def _mine(out):
        return [a for a in out["alerted"] if a["tenant_id"] == tid]

    # First sweep: past the alert bar -> exactly one email for THIS tenant.
    out1 = vip_watch_sweep()
    mine1 = _mine(out1)
    assert mine1 == [{"tenant_id": tid, "array_id": array_id, "vendors": ["sma"]}]
    mine_sent = [s for s in sent if tid in s[1]]
    assert len(mine_sent) == 1
    assert "VIP watch" in mine_sent[0][0]

    with SessionLocal() as db:
        state = db.execute(__import__("sqlalchemy").select(InverterAlertState).where(
            InverterAlertState.tenant_id == tid)).scalar_one()
        assert state.last_alerted_at is not None

    # Second sweep, still stale: deduped, no second email for THIS tenant.
    sent.clear()
    vip_watch_sweep()
    assert [s for s in sent if tid in s[1]] == []

    # Recovers (fresh capture) -> incident clears.
    with SessionLocal() as db:
        inv = db.execute(__import__("sqlalchemy").select(Inverter).where(
            Inverter.array_id == array_id)).scalar_one()
        inv.last_power_at = datetime.utcnow()
        db.commit()
    vip_watch_sweep()
    with SessionLocal() as db:
        assert db.execute(__import__("sqlalchemy").select(InverterAlertState).where(
            InverterAlertState.tenant_id == tid)).scalar_one_or_none() is None


def test_sweep_holds_off_a_plain_tenant_past_the_fast_bar_but_under_the_wide_one(monkeypatch):
    """A plain (non-vip_watch) tenant is now IN the sweep, not skipped — but it
    gets the wide ALERT_AFTER_MINUTES_DEFAULT bar, not the fast VIP one. Stale
    past the fast bar alone must not alert someone who's just closed their
    laptop for lunch."""
    sent = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: sent.append((subject, body)) or True)
    monkeypatch.setattr("api.inverter_fleet._daylight_for", lambda arr, default, _cache=None: True)
    tid = _mk_tenant(vip_watch=False)
    now_ = datetime.utcnow()
    _mk_array_with_inverter(tid, "sma", now_ - timedelta(minutes=ALERT_AFTER_MINUTES_VIP + 30))
    out = vip_watch_sweep()
    assert [s for s in sent if tid in s[1]] == []
    assert [a for a in out["alerted"] if a["tenant_id"] == tid] == []


def test_sweep_eventually_alerts_a_plain_tenant_past_the_wide_bar(monkeypatch):
    """Proves universal coverage: a plain tenant left stale for most of a
    working day DOES eventually alert Ford, just on the wider bar than a
    vip_watch tenant would."""
    sent = []
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda subject, body: sent.append((subject, body)) or True)
    monkeypatch.setattr("api.inverter_fleet._daylight_for", lambda arr, default, _cache=None: True)
    tid = _mk_tenant(vip_watch=False)
    now_ = datetime.utcnow()
    array_id = _mk_array_with_inverter(tid, "sma", now_ - timedelta(minutes=ALERT_AFTER_MINUTES_DEFAULT + 15))
    out = vip_watch_sweep()
    mine = [a for a in out["alerted"] if a["tenant_id"] == tid]
    assert mine == [{"tenant_id": tid, "array_id": array_id, "vendors": ["sma"]}]
    mine_sent = [s for s in sent if tid in s[1]]
    assert len(mine_sent) == 1
    assert "VIP watch" in mine_sent[0][0]
