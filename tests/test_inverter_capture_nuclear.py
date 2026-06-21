"""NUCLEAR tests — Array Operator extension-capture data reliability.

POST /v1/array-owners/inverter-capture is the TRUST SPINE of Array Operator: the
extension reads the owner's logged-in vendor portal (Fronius/SMA/Chint) and ships
readings straight into the per-kWh-billed meter. If this path lies, the product
lies — and because AO bills per kWh, a single poisoned daily row is real money.

test_array_owners.py covers the happy paths (create, idempotent+max, soft-delete
reuse, session-token, vendor-reject, per-inverter rows, site-id rebind, manual
reassignment). This file is the ADVERSARIAL battery for the seams that aren't
covered and that directly protect trust + billing:

  1. PHYSICAL-PLAUSIBILITY CEILING (billing-critical) — a cumulative/lifetime or
     unit-error value must be DROPPED, never metered. This is the guard that
     stopped a 677,533 kWh capture from invoicing ~$4k on a 144 kW array.
  2. CROSS-TENANT ISOLATION — the same physical device captured into two tenants
     must never let one tenant's capture write into the other's rows.
  3. TODAY-IN-BOTH-STREAMS (Sentry PYTHON-FASTAPI-3) — today's energy arriving in
     BOTH the today-field AND the daily[] history must dedupe, not 500.
  4. ADVERSARIAL FIELD DEGRADATION — negatives, nulls, bad dates, blank serials
     must degrade per-field (skip the bad cell), never abort the batch or poison.
  5. DATE INTEGRITY — a timestamped daily entry lands on the correct calendar day.
  6. POWER-ALLOCATION HONESTY — the site's live total is split across inverters by
     a real basis (energy share) and never fabricated when the portal is silent.
"""
from __future__ import annotations

import math
import secrets

import pytest
from sqlalchemy import select

from api.db import SessionLocal
from api.models import (
    Array, DailyGeneration, Inverter, InverterDaily, Tenant, now,
)

CAPTURE = "/v1/array-owners/inverter-capture"
SID = "6c97d4a9-25c3-4ab3-9ab9-a62f0107c53a"   # a stable Fronius PvSystemId


def _mk_tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Nuke", contact_email=f"{key}@t.test",
            tenant_key=key, plan="standard", active=True,
            product="array_operator",
        ))
        db.commit()
    return tid, key


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _today() -> str:
    """Match the endpoint's notion of 'today' (server now)."""
    return now().date().isoformat()


def _daily_rows(tid: str) -> list[DailyGeneration]:
    with SessionLocal() as db:
        return db.execute(
            select(DailyGeneration).where(DailyGeneration.tenant_id == tid)
        ).scalars().all()


# ════════════════════════ 1. PLAUSIBILITY CEILING (billing) ═══════════════════

def test_array_cumulative_glitch_is_dropped_not_metered(client):
    """The exact $4k-bill scenario: a 144 kW array reports 677,533 kWh "today"
    (a lifetime/cumulative value leaked into a daily slot, ~34× the 24h-flat-out
    physical max). The array may still be created, but NO DailyGeneration row is
    written — the per-kWh meter stays clean."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Waterford", "peak_power_kw": 144.0,
        "energy_today_kwh": 677533.0, "status": "producing",
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    # The array exists, but the implausible day was dropped — meter is empty.
    assert _daily_rows(tid) == []


def test_array_strong_but_plausible_day_is_kept(client):
    """A genuinely strong day JUST UNDER the ceiling (144 kW × 24h = 3456) must
    NOT be dropped — the guard only catches impossible garbage, never real
    production."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Waterford", "peak_power_kw": 144.0,
        "energy_today_kwh": 3000.0, "status": "producing",
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    rows = _daily_rows(tid)
    assert len(rows) == 1 and math.isclose(rows[0].kwh, 3000.0)


def test_array_ceiling_boundary_exact_kept_over_dropped(client):
    """At the ceiling: exactly peak×24 is kept; one kWh over is dropped."""
    tid, key = _mk_tenant()
    # ceiling = 10 kW × 24 = 240
    base = {"site_id": SID, "name": "Edge", "peak_power_kw": 10.0, "status": "ok"}
    client.post(CAPTURE, json={"provider": "fronius",
                "sites": [dict(base, energy_today_kwh=240.0)]}, headers=_auth(key))
    rows = _daily_rows(tid)
    assert len(rows) == 1 and math.isclose(rows[0].kwh, 240.0)  # exact = kept

    tid2, key2 = _mk_tenant()
    client.post(CAPTURE, json={"provider": "fronius",
                "sites": [dict(base, energy_today_kwh=240.01)]}, headers=_auth(key2))
    assert _daily_rows(tid2) == []                              # over = dropped


def test_per_inverter_cumulative_glitch_dropped_sibling_kept(client):
    """The same Fronius glitch wrote ~36,000 kWh into each 7.6 kW inverter's daily
    slot (ceiling ≈ 182). The implausible inverter-day must be dropped from
    InverterDaily (so the sparkline + peer-analysis never see an impossible
    spike), while a plausible sibling inverter is recorded normally."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Waterford", "peak_power_kw": 50.0,
        "energy_today_kwh": 300.0, "status": "producing",
        "inverters": [
            {"serial": "glitch", "nameplate_kw": 7.6, "energy_today_kwh": 36000.0},
            {"serial": "healthy", "nameplate_kw": 7.6, "energy_today_kwh": 40.0},
        ],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        # Both inverter ROWS exist (identity is real); only the bad DAILY is dropped.
        invs = {iv.serial: iv for iv in db.execute(
            select(Inverter).where(Inverter.tenant_id == tid)).scalars().all()}
        assert set(invs) == {"glitch", "healthy"}
        glitch_daily = db.execute(select(InverterDaily).where(
            InverterDaily.inverter_id == invs["glitch"].id)).scalars().all()
        healthy_daily = db.execute(select(InverterDaily).where(
            InverterDaily.inverter_id == invs["healthy"].id)).scalars().all()
        assert glitch_daily == []                                  # impossible → dropped
        assert len(healthy_daily) == 1 and math.isclose(healthy_daily[0].kwh, 40.0)


def test_ceiling_uses_inverter_nameplates_when_no_site_peak(client):
    """No site peak_power_kw — the ceiling falls back to the SUM of the inverters'
    nameplates × 24h. Two 5 kW inverters → 10 kW → 240 ceiling; a 5,000 kWh day
    is dropped, a 200 kWh day kept."""
    tid, key = _mk_tenant()
    invs = [{"serial": "a", "nameplate_kw": 5.0}, {"serial": "b", "nameplate_kw": 5.0}]
    client.post(CAPTURE, json={"provider": "fronius", "sites": [{
        "site_id": SID, "name": "NoPeak", "energy_today_kwh": 5000.0,
        "inverters": invs}]}, headers=_auth(key))
    assert _daily_rows(tid) == []  # 5000 > 10kW×24=240 → dropped

    tid2, key2 = _mk_tenant()
    client.post(CAPTURE, json={"provider": "fronius", "sites": [{
        "site_id": SID, "name": "NoPeak", "energy_today_kwh": 200.0,
        "inverters": invs}]}, headers=_auth(key2))
    rows = _daily_rows(tid2)
    assert len(rows) == 1 and math.isclose(rows[0].kwh, 200.0)


@pytest.mark.xfail(reason="KNOWN GAP (flagged to Ford): with NO site peak AND NO "
                          "inverter nameplate, day_ceiling is None so the "
                          "plausibility guard is disabled and a cumulative/unit-"
                          "error value reaches the per-kWh meter. A conservative "
                          "absolute fallback ceiling would close this.",
                   strict=True)
def test_no_nameplate_no_peak_still_guards_cumulative_glitch(client):
    """DESIRED contract: even when the capture omits BOTH peak_power_kw and every
    nameplate, an obviously-cumulative value (677,533 kWh in one day) must not be
    metered. Currently it IS (ceiling=None disables the guard) — this xfail is the
    tripwire that flips green the moment a fallback ceiling is added."""
    tid, key = _mk_tenant()
    client.post(CAPTURE, json={"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Bare", "energy_today_kwh": 677533.0,
        "inverters": [{"serial": "x"}]}]}, headers=_auth(key))
    assert _daily_rows(tid) == []  # currently fails: the glitch is metered


# ════════════════════════ 2. CROSS-TENANT ISOLATION ══════════════════════════

def test_capture_is_strictly_tenant_isolated_for_shared_serial(client):
    """The same physical inverter (vendor+serial) can be captured into two
    tenants (installer + owner both run the extension on one Solar.web system).
    Each tenant must get its OWN Inverter row and its OWN meter — tenant B's
    capture must NEVER mutate tenant A's row, array, or DailyGeneration."""
    tid_a, key_a = _mk_tenant()
    tid_b, key_b = _mk_tenant()
    # Nameplate (30 kW) keeps both readings physically plausible (< 30×24h) so this
    # isolation test never seeds a watchdog-impossible row regardless of test order.
    shared = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Shared System", "peak_power_kw": 30.0,
        "energy_today_kwh": 100.0,
        "inverters": [{"serial": "shared-1", "nameplate_kw": 30.0,
                       "energy_today_kwh": 100.0, "current_power_w": 5000.0}],
    }]}
    client.post(CAPTURE, json=shared, headers=_auth(key_a))

    # Snapshot A's inverter before B captures.
    with SessionLocal() as db:
        a_iv = db.execute(select(Inverter).where(
            Inverter.tenant_id == tid_a)).scalar_one()
        a_iv_id, a_power, a_array = a_iv.id, a_iv.last_power_w, a_iv.array_id

    # Tenant B captures the SAME serial with a DIFFERENT reading.
    other = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Shared System", "peak_power_kw": 30.0,
        "energy_today_kwh": 250.0,
        "inverters": [{"serial": "shared-1", "nameplate_kw": 30.0,
                       "energy_today_kwh": 250.0, "current_power_w": 9000.0}],
    }]}
    client.post(CAPTURE, json=other, headers=_auth(key_b))

    with SessionLocal() as db:
        # Exactly one inverter row PER tenant for that serial — never shared.
        rows = db.execute(select(Inverter).where(
            Inverter.serial == "shared-1")).scalars().all()
        by_tenant = {iv.tenant_id: iv for iv in rows}
        assert set(by_tenant) == {tid_a, tid_b}
        assert by_tenant[tid_a].id != by_tenant[tid_b].id
        # A's row is byte-for-byte untouched by B's capture.
        a_after = db.get(Inverter, a_iv_id)
        assert a_after.last_power_w == a_power and a_after.array_id == a_array
        # A's meter shows A's number; B's shows B's — no cross-write.
        a_kwh = db.execute(select(DailyGeneration.kwh).where(
            DailyGeneration.tenant_id == tid_a)).scalars().all()
        b_kwh = db.execute(select(DailyGeneration.kwh).where(
            DailyGeneration.tenant_id == tid_b)).scalars().all()
        assert [round(x, 2) for x in a_kwh] == [100.0]
        assert [round(x, 2) for x in b_kwh] == [250.0]


# ════════════════════ 3. TODAY-IN-BOTH-STREAMS (Sentry FP-3) ══════════════════

def test_today_in_today_field_and_daily_history_no_crash(client):
    """Sentry PYTHON-FASTAPI-3: today's energy arrives in BOTH energy_today_kwh
    AND a daily[] entry dated today (SMA's series includes today). The old
    SELECT-then-INSERT raised UniqueViolation at commit. Must 200 with exactly
    ONE row for today = max of the two values — array AND per-inverter."""
    tid, key = _mk_tenant()
    today = _today()
    payload = {"provider": "sma", "sites": [{
        "site_id": SID, "name": "DupDay", "peak_power_kw": 30.0,
        "energy_today_kwh": 120.0,
        "daily": [{"date": today, "kwh": 130.0}],   # same day, higher
        "inverters": [{
            "serial": "inv-1", "nameplate_kw": 15.0,
            "energy_today_kwh": 60.0,
            "daily": [{"date": today, "kwh": 65.0}],
        }],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text   # NOT a 500 UniqueViolation
    with SessionLocal() as db:
        arr_rows = db.execute(select(DailyGeneration).where(
            DailyGeneration.tenant_id == tid,
            DailyGeneration.day == now().date())).scalars().all()
        assert len(arr_rows) == 1 and math.isclose(arr_rows[0].kwh, 130.0)  # max-wins
        inv_rows = db.execute(select(InverterDaily).where(
            InverterDaily.tenant_id == tid,
            InverterDaily.day == now().date())).scalars().all()
        assert len(inv_rows) == 1 and math.isclose(inv_rows[0].kwh, 65.0)   # max-wins


def test_duplicate_dates_within_daily_history_dedupe(client):
    """A daily[] history that itself repeats a date (vendor quirk) must collapse
    to one row at the MAX value — never a UniqueViolation, never a double-count."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "DupHist", "peak_power_kw": 30.0,
        "daily": [
            {"date": "2026-06-18", "kwh": 100.0},
            {"date": "2026-06-18", "kwh": 140.0},   # dup date, higher
            {"date": "2026-06-18", "kwh": 90.0},    # dup date, lower
        ],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        rows = db.execute(select(DailyGeneration).where(
            DailyGeneration.tenant_id == tid)).scalars().all()
        assert len(rows) == 1 and math.isclose(rows[0].kwh, 140.0)


# ════════════════════ 4. ADVERSARIAL FIELD DEGRADATION ════════════════════════

def test_malformed_fields_degrade_per_field_never_500(client):
    """Negatives, nulls, a bad date string, a blank serial, and a negative live
    power in ONE payload must each be skipped at the field level — the request
    still 200s, the array is created, and nothing garbage is metered or stamped."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Messy", "peak_power_kw": 40.0,
        "energy_today_kwh": -5.0,                      # negative → skipped
        "current_power_w": -1234.0,                    # negative → not stamped
        "daily": [
            {"date": "not-a-date", "kwh": 50.0},       # bad date → skipped
            {"date": "2026-06-18", "kwh": None},       # null kwh → skipped
            {"date": "2026-06-18", "kwh": -9.0},       # negative → skipped
            {"date": "2026-06-17", "kwh": 33.0},       # the ONE good row
        ],
        "inverters": [
            {"serial": "   ", "energy_today_kwh": 10.0},     # blank serial → no row
            {"serial": "ok-1", "nameplate_kw": 20.0, "energy_today_kwh": 30.0},
        ],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        # Exactly one good daily row survived; the garbage was skipped, not stored.
        rows = db.execute(select(DailyGeneration).where(
            DailyGeneration.tenant_id == tid)).scalars().all()
        assert len(rows) == 1 and math.isclose(rows[0].kwh, 33.0)
        # Blank-serial inverter never created; only the real one.
        invs = db.execute(select(Inverter).where(
            Inverter.tenant_id == tid)).scalars().all()
        assert [iv.serial for iv in invs] == ["ok-1"]
        # A negative live reading must never be stamped as power.
        assert invs[0].last_power_w is None or invs[0].last_power_w >= 0


def test_negative_inverter_energy_not_metered(client):
    """A negative per-inverter energy must not create an InverterDaily row."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Neg", "peak_power_kw": 20.0,
        "inverters": [{"serial": "n1", "nameplate_kw": 10.0,
                       "energy_today_kwh": -3.0}],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        inv = db.execute(select(Inverter).where(
            Inverter.tenant_id == tid)).scalar_one()
        daily = db.execute(select(InverterDaily).where(
            InverterDaily.inverter_id == inv.id)).scalars().all()
        assert daily == []


# ════════════════════════════ 5. DATE INTEGRITY ══════════════════════════════

def test_timestamped_daily_date_lands_on_correct_calendar_day(client):
    """A daily[] entry carrying a full timestamp must be attributed to its
    calendar DATE — not crash, not drift to an adjacent day (which would
    double-count or leave a gap in the per-kWh meter)."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Stamped", "peak_power_kw": 30.0,
        "daily": [{"date": "2026-06-18T23:30:00Z", "kwh": 77.0}],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        row = db.execute(select(DailyGeneration).where(
            DailyGeneration.tenant_id == tid)).scalar_one()
        assert row.day.isoformat() == "2026-06-18"
        assert math.isclose(row.kwh, 77.0)


# ════════════════════════ 6. POWER-ALLOCATION HONESTY ═════════════════════════

def test_site_power_split_by_energy_share_sums_to_measured_total(client):
    """The portal gives ONE site-level "now" reading; we split it across inverters
    by each one's share of TODAY's energy so the per-inverter values SUM to the
    measured site total — a principled split, never an invented number."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Split", "peak_power_kw": 60.0,
        "energy_today_kwh": 100.0, "current_power_w": 30000.0,
        "inverters": [
            {"serial": "p1", "nameplate_kw": 20.0, "energy_today_kwh": 60.0},
            {"serial": "p2", "nameplate_kw": 20.0, "energy_today_kwh": 40.0},
        ],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        invs = {iv.serial: iv for iv in db.execute(select(Inverter).where(
            Inverter.tenant_id == tid)).scalars().all()}
        # 60:40 energy share of 30,000 W = 18,000 / 12,000, summing to the total.
        assert math.isclose(invs["p1"].last_power_w, 18000.0, abs_tol=0.5)
        assert math.isclose(invs["p2"].last_power_w, 12000.0, abs_tol=0.5)
        total = invs["p1"].last_power_w + invs["p2"].last_power_w
        assert math.isclose(total, 30000.0, abs_tol=1.0)


def test_per_device_reading_preferred_over_allocation(client):
    """When the portal exposes a REAL per-inverter reading (Chint commDevice),
    that measured value wins over the derived site-split — we never overwrite a
    true reading with an estimate."""
    tid, key = _mk_tenant()
    payload = {"provider": "chint", "sites": [{
        "site_id": SID, "name": "PerDev", "peak_power_kw": 40.0,
        "energy_today_kwh": 100.0, "current_power_w": 30000.0,
        "inverters": [
            {"serial": "c1", "nameplate_kw": 20.0, "energy_today_kwh": 50.0,
             "current_power_w": 7777.0},                 # real per-device reading
            {"serial": "c2", "nameplate_kw": 20.0, "energy_today_kwh": 50.0},
        ],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        c1 = db.execute(select(Inverter).where(
            Inverter.tenant_id == tid, Inverter.serial == "c1")).scalar_one()
        assert math.isclose(c1.last_power_w, 7777.0)     # measured, not allocated


def test_no_site_power_stamps_no_live_reading(client):
    """When the portal reports NO live power, we stamp nothing — the card shows an
    honest "—" rather than a fabricated 0 or a stale number."""
    tid, key = _mk_tenant()
    payload = {"provider": "fronius", "sites": [{
        "site_id": SID, "name": "Dark", "peak_power_kw": 30.0,
        "energy_today_kwh": 50.0, "current_power_w": None,
        "inverters": [{"serial": "d1", "nameplate_kw": 15.0,
                       "energy_today_kwh": 50.0}],
    }]}
    r = client.post(CAPTURE, json=payload, headers=_auth(key))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        d1 = db.execute(select(Inverter).where(
            Inverter.tenant_id == tid)).scalar_one()
        assert d1.last_power_w is None
