"""Vendor-feed durability — kill the MOLE-CLASS, not the symptom.

Ford reports feeds breaking "one vendor at a time" (SMA, then Fronius, then a
SolarEdge that reads live while its own portal says OFFLINE). The durable lesson
(docs/knowledge/live-inverter-feeds-whack-a-mole.md): most of these are the SAME
class of bug, and the fix is honest per-vendor-CLASS handling, not another patch.

This file pins the two vendor classes so a future change can't silently
reintroduce the mole. It tests CONTRACTS, organized by class:

  CLASS A — API-PULLED  (SolarEdge, Locus/AlsoEnergy)
    Real server-pullable creds (api_key+site_id, or client creds). The poller
    refreshes them continuously. Their live value is trustworthy ONLY while its
    own telemetry timestamp is fresh — a frozen reading from a source that
    stopped reporting must be dropped (the Cover Catamount contradiction).

  CLASS B — EXTENSION-ONLY  (SMA, Fronius, Chint)
    Zero pullable creds — the extension's hourly recapture is their ONLY source.
    The server poller MUST NOT claim to refresh them: _pullable_connection must
    return None so poll_all_sources never calls fetch_live, never writes a
    reading, and never stamps last_sync_at (a false "refreshed just now"). Their
    card power comes from the capture-time fallback, daylight+freshness gated.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import api.inverter_fleet as fleet
from api import poller
from api import inverters as vendors
from api.models import Inverter, now


# Vendor-class taxonomy (the brief's, encoded as the code's ground truth).
API_PULLED = ["solaredge", "locus"]
EXTENSION_ONLY = ["sma", "fronius", "chint"]


def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _iv(vendor: str, serial: str = "S", last_power_w=None, fresh: bool = True):
    iv = Inverter(tenant_id="t", array_id=1, vendor=vendor, serial=serial)
    iv.last_power_w = last_power_w
    iv.last_power_at = now() if fresh else None
    return iv


# ─────────────────────── CLASS A — API-PULLED ────────────────────────────────

@pytest.mark.parametrize("vendor", API_PULLED)
def test_api_pulled_stale_live_value_dropped(vendor):
    """A vendor live reading whose OWN last_report is stale is a frozen value
    from a source that stopped reporting — it must NOT read as live (it would
    contradict the SOURCE-OFFLINE banner). Holds for every API-pulled vendor."""
    iv = _iv(vendor, "a1")
    m = {"last_power_w": 596.3, "last_report": _iso_ago(8.0)}  # 8h old → stale
    assert fleet._live_power_w(iv, m, daylight=True) is None


@pytest.mark.parametrize("vendor", API_PULLED)
def test_api_pulled_fresh_live_value_kept(vendor):
    """The same reading with a fresh report IS genuinely live → keep it."""
    iv = _iv(vendor, "a1")
    m = {"last_power_w": 596.3, "last_report": _iso_ago(1.0)}  # 1h old → fresh
    assert fleet._live_power_w(iv, m, daylight=True) == 596.3


@pytest.mark.parametrize("vendor", API_PULLED)
def test_api_pulled_connection_is_server_pullable(vendor):
    """With real creds, an API-pulled vendor IS pullable (the poller refreshes
    it). SolarEdge: api_key+site_id. Locus: client creds."""
    cfg = ({"api_key": "k", "site_id": 1} if vendor == "solaredge"
           else {"client_id": "c", "client_secret": "s", "site_id": 1})
    conn = types.SimpleNamespace(id=1, vendor=vendor, config=cfg)
    arr = types.SimpleNamespace(_conn=conn)
    orig = poller._fleet._resolve_connection
    poller._fleet._resolve_connection = lambda db, a: a._conn
    try:
        assert poller._pullable_connection(None, arr) is conn
    finally:
        poller._fleet._resolve_connection = orig


# ─────────────────────── CLASS B — EXTENSION-ONLY ────────────────────────────

def test_chint_is_extension_only_by_design():
    """Chint is SUPPORTS_LIVE=False: its only feed is the extension's weekETrend
    recapture, never a server pull. This is the regression lock — if someone
    flips the flag, the poller would start hitting Chint and falsely 'refresh'
    it. Even WITH api_key+site_id present, it must stay non-pullable."""
    assert vendors.VENDORS["chint"].SUPPORTS_LIVE is False
    conn = types.SimpleNamespace(id=7, vendor="chint",
                                 config={"api_key": "k", "site_id": 1})
    arr = types.SimpleNamespace(_conn=conn)
    orig = poller._fleet._resolve_connection
    poller._fleet._resolve_connection = lambda db, a: a._conn
    try:
        assert poller._pullable_connection(None, arr) is None
    finally:
        poller._fleet._resolve_connection = orig


@pytest.mark.parametrize("vendor", ["sma", "fronius"])
def test_extension_only_without_creds_not_pullable(vendor):
    """SMA/Fronius CAN be pulled in principle (SUPPORTS_LIVE=True) but only once
    Ford registers a vendor dev app. An extension capture stores a portal/site
    marker with NO pullable creds (no api_key, no client creds, no refresh
    token) → the poller must skip it, not pretend to refresh it."""
    for cfg in ({}, {"site_id": "PV-12345"}):  # faithful: site marker, no creds
        conn = types.SimpleNamespace(id=3, vendor=vendor, config=cfg)
        arr = types.SimpleNamespace(_conn=conn)
        orig = poller._fleet._resolve_connection
        poller._fleet._resolve_connection = lambda db, a: a._conn
        try:
            assert poller._pullable_connection(None, arr) is None
        finally:
            poller._fleet._resolve_connection = orig


@pytest.mark.parametrize("vendor", EXTENSION_ONLY)
def test_extension_only_uses_capture_fallback_daylight_gated(vendor):
    """No live telemetry (m carries no last_power_w) — the card power comes from
    the capture-time fallback iv.last_power_w, shown in daylight but hidden at
    night so a 2pm capture never reads as 'producing' at 9pm (the SMA bug)."""
    iv = _iv(vendor, "x", last_power_w=17000.0, fresh=True)
    assert fleet._live_power_w(iv, {}, daylight=True) == 17000.0   # day: shown
    assert fleet._live_power_w(iv, {}, daylight=False) is None      # night: hidden


def test_poller_does_not_refresh_extension_only_array(monkeypatch):
    """END-TO-END durability contract: an extension-only array (Fronius capture,
    no pullable creds) survives a full poll run WITHOUT being touched — no
    fetch_live call against its config, no InverterReading written, and crucially
    its connection.last_sync_at stays None (the poller never claims a refresh it
    didn't do). It's simply counted as skipped."""
    from api.db import SessionLocal
    from api.models import Tenant, Array, InverterConnection, InverterReading

    poller._reset_budget()
    tid = "ten_" + uuid.uuid4().hex[:16]
    marker = "EXT-" + uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="ExtT", contact_email=f"{tid}@x.test",
                      tenant_key="sol_live_" + uuid.uuid4().hex, plan="standard",
                      active=True, product="array_operator"))
        arr = Array(tenant_id=tid, name="Fronius site")
        db.add(arr); db.flush()
        # Extension-captured Fronius: site marker, NO api_key / client creds.
        conn = InverterConnection(array_id=arr.id, vendor="fronius",
                                  config={"site_id": marker}, status="ok")
        db.add(conn); db.flush()
        db.add(Inverter(tenant_id=tid, array_id=arr.id, vendor="fronius",
                        serial=marker + "-A", source_connection_id=conn.id,
                        source_array_id=arr.id, nameplate_kw=8.0))
        db.commit()
        conn_id = conn.id

    seen_configs = []

    def spy_fetch_live(vendor, cfg):
        seen_configs.append(cfg)
        # Never make a real network call for any other array in the shared DB.
        return {"current_power_w": None, "as_of": None}

    monkeypatch.setattr(poller._vendors, "fetch_live", spy_fetch_live)
    poller.poll_all_sources(force_daylight=True)

    # The extension array's config was NEVER handed to fetch_live.
    assert all(c.get("site_id") != marker for c in seen_configs)
    with SessionLocal() as db:
        c = db.get(InverterConnection, conn_id)
        assert c.last_sync_at is None          # no false "refreshed just now"
        n = db.query(InverterReading).filter(
            InverterReading.tenant_id == tid).count()
        assert n == 0                          # nothing written for it
