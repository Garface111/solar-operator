"""Tests for the data-hub poller: the daylight honesty gate, the site-level
fetch_live poll path, nameplate allocation, and the per-credential budget
governor that keeps us under SolarEdge's 300-req/day/key cap."""
import types
from datetime import timedelta, date

import api.inverter_fleet as fleet
from api.inverter_fleet import _live_power_w, _POWER_FRESH
from api.models import Inverter, now
from api import poller


def _inv(pw, age_hours):
    iv = Inverter(tenant_id="t", array_id=1, vendor="sma", serial="X")
    iv.last_power_w = pw
    iv.last_power_at = now() - timedelta(hours=age_hours)
    return iv


# ── honesty gate (unchanged behavior, the original Tannery Brook bug) ─────────

def test_live_value_always_trusted():
    iv = _inv(None, 0)
    assert _live_power_w(iv, {"last_power_w": 5000.0}, daylight=False) == 5000.0
    assert _live_power_w(iv, {"last_power_w": 5000.0}, daylight=True) == 5000.0


def test_stale_capture_fresh_daytime_shows():
    iv = _inv(17000.0, 2)
    assert _live_power_w(iv, {}, daylight=True) == 17000.0


def test_stale_capture_at_night_is_hidden():
    # THE BUG FIX: a 2pm capture must NOT read as producing at night.
    iv = _inv(17000.0, 6)
    assert _live_power_w(iv, {}, daylight=False) is None


def test_capture_beyond_freshness_window_hidden():
    iv = _inv(17000.0, _POWER_FRESH.total_seconds() / 3600 + 1)
    assert _live_power_w(iv, {}, daylight=True) is None


# ── poll gating ───────────────────────────────────────────────────────────────

def test_poller_skips_when_dark(monkeypatch):
    monkeypatch.setattr(poller._fleet, "_is_daylight", lambda: False)
    summary = poller.poll_all_sources()
    assert summary["ran"] is False
    assert summary["daylight"] is False


def test_pullable_connection_requires_creds_and_live_support():
    # api_key + site_id on a SUPPORTS_LIVE vendor → pullable
    c1 = types.SimpleNamespace(id=1, config={"api_key": "k", "site_id": 1}, vendor="solaredge")
    # oauth creds on a SUPPORTS_LIVE vendor → pullable
    c2 = types.SimpleNamespace(id=2, config={"refresh_token": "r"}, vendor="sma")
    # no creds → not pullable
    c3 = types.SimpleNamespace(id=3, config={}, vendor="fronius")
    # unknown vendor → not pullable (no live support)
    c4 = types.SimpleNamespace(id=4, config={"api_key": "k", "site_id": 1}, vendor="bogus")
    # Fronius access-key creds → pullable (regression: this branch was missing
    # entirely, so every real official-API Fronius connection — e.g. Bruce's
    # live Waterford/Chester key — was silently invisible to the poller
    # despite fetch_live working fine).
    c5 = types.SimpleNamespace(
        id=5, config={"access_key_id": "aki", "access_key_value": "akv",
                      "pv_system_id": "sid"}, vendor="fronius")

    def fake_resolve(db, arr):
        return arr._conn

    orig = poller._fleet._resolve_connection
    poller._fleet._resolve_connection = fake_resolve
    try:
        assert poller._pullable_connection(None, types.SimpleNamespace(_conn=c1)) is c1
        assert poller._pullable_connection(None, types.SimpleNamespace(_conn=c2)) is c2
        assert poller._pullable_connection(None, types.SimpleNamespace(_conn=c3)) is None
        assert poller._pullable_connection(None, types.SimpleNamespace(_conn=c4)) is None
        assert poller._pullable_connection(None, types.SimpleNamespace(_conn=None)) is None
        assert poller._pullable_connection(None, types.SimpleNamespace(_conn=c5)) is c5
    finally:
        poller._fleet._resolve_connection = orig


def test_credential_key_scopes_fronius_by_access_key_id():
    """Two different Fronius customers' keys must land in DIFFERENT budget
    buckets — the old fallback (no fronius-specific branch) hashed every
    Fronius connection to the same blank "fronius:" bucket, so one customer's
    polling could throttle another's."""
    assert poller._credential_key("fronius", {"access_key_id": "AAA", "access_key_value": "x"}) == "fronius:AAA"
    assert (poller._credential_key("fronius", {"access_key_id": "AAA", "access_key_value": "x"})
            != poller._credential_key("fronius", {"access_key_id": "BBB", "access_key_value": "y"}))


# ── nameplate allocation ──────────────────────────────────────────────────────

def test_allocate_power_by_nameplate_share():
    a = Inverter(tenant_id="t", array_id=1, vendor="solaredge", serial="A")
    a.id, a.nameplate_kw = 1, 30.0
    b = Inverter(tenant_id="t", array_id=1, vendor="solaredge", serial="B")
    b.id, b.nameplate_kw = 2, 10.0
    alloc = poller._allocate_power([a, b], 4000.0)
    # 30:10 split of 4000 W = 3000 / 1000
    assert alloc[1] == 3000.0
    assert alloc[2] == 1000.0
    assert round(sum(alloc.values()), 1) == 4000.0


def test_allocate_power_equal_when_no_nameplate():
    a = Inverter(tenant_id="t", array_id=1, vendor="solaredge", serial="A"); a.id = 1
    b = Inverter(tenant_id="t", array_id=1, vendor="solaredge", serial="B"); b.id = 2
    alloc = poller._allocate_power([a, b], 5000.0)
    assert alloc[1] == 2500.0 and alloc[2] == 2500.0


def test_allocate_power_handles_none_and_empty():
    a = Inverter(tenant_id="t", array_id=1, vendor="solaredge", serial="A"); a.id = 1
    assert poller._allocate_power([a], None) == {}
    assert poller._allocate_power([], 1000.0) == {}


# ── budget governor (the scaling fix) ─────────────────────────────────────────

def test_governor_enforces_daily_ceiling():
    poller._reset_budget()
    day = date(2026, 6, 17)
    ts = now()
    ck = "solaredge:KEY"
    allowed = 0
    # Bypass spacing by advancing ts far past the interval each iteration.
    for i in range(poller.DAILY_BUDGET_PER_KEY + 50):
        t = ts + timedelta(hours=i)  # always past min interval
        if poller._governor_allows(ck, day, t, sites_under_key=1):
            poller._governor_record(ck, day, t)
            allowed += 1
    assert allowed == poller.DAILY_BUDGET_PER_KEY


def test_governor_enforces_spacing_interval():
    poller._reset_budget()
    day = date(2026, 6, 17)
    ts = now()
    ck = "solaredge:KEY2"
    # First poll always allowed.
    assert poller._governor_allows(ck, day, ts, sites_under_key=1)
    poller._governor_record(ck, day, ts)
    # Immediately after, the same key is throttled (hasn't waited the interval).
    assert poller._governor_allows(ck, day, ts + timedelta(seconds=1), sites_under_key=1) is False
    # After the min interval it's allowed again.
    iv = poller._min_interval_seconds(1)
    assert poller._governor_allows(ck, day, ts + timedelta(seconds=iv + 1), sites_under_key=1)


def test_governor_spacing_scales_with_sites_per_key():
    # More sites under one key → longer spacing per site (adaptive cadence).
    one = poller._min_interval_seconds(1)
    many = poller._min_interval_seconds(10)
    assert many > one
    # A single site polls at least every 5 min (tight cadence) on a key alone.
    assert one <= 5 * 60 + 1


def test_governor_budget_lasts_the_whole_daylight_span():
    # Sanity: at the derived spacing, a key cannot exceed its budget across the
    # longest modeled daylight span.
    sites = 4
    iv = poller._min_interval_seconds(sites)
    polls_per_site = poller._MAX_DAYLIGHT_SECONDS / iv
    total_calls = polls_per_site * sites
    assert total_calls <= poller.DAILY_BUDGET_PER_KEY + 1


def test_governor_resets_on_new_day():
    poller._reset_budget()
    ts = now()
    ck = "solaredge:KEY3"
    st = poller._governor(ck, date(2026, 6, 17))
    st["calls"] = poller.DAILY_BUDGET_PER_KEY  # exhausted
    assert poller._governor_allows(ck, date(2026, 6, 17), ts, 1) is False
    # New day → fresh budget.
    assert poller._governor_allows(ck, date(2026, 6, 18), ts, 1) is True


# ── end-to-end poll path (real DB rows, stubbed vendor) ───────────────────────

def test_poll_all_sources_site_level_and_allocates(monkeypatch):
    """ONE fetch_live call per site; site power split across inverters by
    nameplate; InverterReading rows written; last_power_w refreshed."""
    import uuid
    from api.db import SessionLocal
    from api.models import (
        Tenant, Array, InverterConnection, Inverter, InverterReading,
    )

    poller._reset_budget()
    tid = "ten_" + uuid.uuid4().hex[:16]
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="T", contact_email=f"{tid}@x.test",
                      tenant_key="sol_live_" + uuid.uuid4().hex, plan="standard",
                      active=True, product="array_operator"))
        arr = Array(tenant_id=tid, name="Site A")
        db.add(arr)
        db.flush()
        conn = InverterConnection(
            array_id=arr.id, vendor="solaredge",
            config={"api_key": "K-E2E", "site_id": 999}, status="ok",
        )
        db.add(conn)
        db.flush()
        a = Inverter(tenant_id=tid, array_id=arr.id, vendor="solaredge",
                     serial="E2E-A", source_connection_id=conn.id,
                     source_array_id=arr.id, nameplate_kw=30.0)
        b = Inverter(tenant_id=tid, array_id=arr.id, vendor="solaredge",
                     serial="E2E-B", source_connection_id=conn.id,
                     source_array_id=arr.id, nameplate_kw=10.0)
        db.add_all([a, b])
        db.commit()
        arr_id, a_id, b_id = arr.id, a.id, b.id

    calls = {"n": 0, "mine": 0}

    def fake_fetch_live(vendor, cfg):
        calls["n"] += 1
        if cfg.get("api_key") == "K-E2E":
            calls["mine"] += 1
            return {"current_power_w": 4000.0, "as_of": "2026-06-17T18:00:00"}
        # Any other leftover array in the shared test DB: return null so it
        # writes nothing and never makes a real network call.
        return {"current_power_w": None, "as_of": None}

    monkeypatch.setattr(poller._vendors, "fetch_live", fake_fetch_live)

    summary = poller.poll_all_sources(force_daylight=True)

    # My 2-inverter site was polled with exactly ONE site-level call (the
    # scaling win — not 1 inventory + N equipment calls).
    assert calls["mine"] == 1
    assert summary["readings_written"] >= 2

    with SessionLocal() as db:
        rows = db.query(InverterReading).filter(
            InverterReading.inverter_id.in_([a_id, b_id])
        ).all()
        by_inv = {r.inverter_id: r.power_w for r in rows}
        # 30:10 nameplate split of 4000 W.
        assert by_inv[a_id] == 3000.0
        assert by_inv[b_id] == 1000.0
        a2 = db.get(Inverter, a_id)
        assert a2.last_power_w == 3000.0
        assert a2.last_power_at is not None


def test_poll_all_sources_skips_null_power(monkeypatch):
    """A vendor returning null power consumes a budget call but writes no rows."""
    import uuid
    from api.db import SessionLocal
    from api.models import Tenant, Array, InverterConnection, Inverter, InverterReading

    poller._reset_budget()
    tid = "ten_" + uuid.uuid4().hex[:16]
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="T2", contact_email=f"{tid}@x.test",
                      tenant_key="sol_live_" + uuid.uuid4().hex, plan="standard",
                      active=True, product="array_operator"))
        arr = Array(tenant_id=tid, name="Site B")
        db.add(arr); db.flush()
        conn = InverterConnection(array_id=arr.id, vendor="solaredge",
                                  config={"api_key": "K-NULL", "site_id": 1}, status="ok")
        db.add(conn); db.flush()
        db.add(Inverter(tenant_id=tid, array_id=arr.id, vendor="solaredge",
                        serial="NULL-A", source_connection_id=conn.id,
                        source_array_id=arr.id, nameplate_kw=5.0))
        db.commit()

    monkeypatch.setattr(poller._vendors, "fetch_live",
                        lambda v, c: {"current_power_w": None, "as_of": None})
    summary = poller.poll_all_sources(force_daylight=True)
    assert summary["api_calls"] >= 1       # call(s) still spent
    assert summary["readings_written"] == 0  # but nothing written
