"""An inverter with a dead energy register must not display invented watts.

Bruce's Tannery Brook #7 (SMA, S/N 191213319) is the live case. SMA's own Sunny
Portal shows it at 1,256 W with "No data available" for both Current month and
Current year — its cumulative-energy (TotWhOut) register is dead, so it has no
history anywhere, in any tenant. Our spreadsheet showed it at "~8.3 kW", the
HIGHEST of its seven siblings, which is exactly backwards: the one unit with a
real metering fault rendered as the site's best performer.

8.3 kW was 8267.6 W borrowed off ten_ford_demo_100 — a SEEDED demo tenant that
happens to carry the same SMA serial. The borrow already tried to exclude demo
tenants, but only via `tenant_id LIKE 'ten_demo%'`, which does not match
'ten_ford_demo_100' (or 'ten_anna_800', which carries 12127.2 W on that serial).
rate_schedule.SYNTHETIC_TENANT_IDS already named ten_ford_demo_100 as synthetic
for the money path; the live-power path had its own weaker guess.

Two independent guarantees:
  1. demo/seed tenants can never lend a reading (test_borrow_*)
  2. a dead-register unit shows only its OWN measurement, never a derived one
     (test_blank_*)
"""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest

from api.db import SessionLocal, init_db
from api.models import Array, Inverter, Tenant, now
from api import inverter_fleet as fleet
from api.rate_schedule import SYNTHETIC_TENANT_IDS

SERIAL = "191213319"


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _row(**over):
    r = {
        "inverter_id": 1, "sn": SERIAL, "vendor": "sma",
        "no_energy_register": True, "current_power_w": 8267.6,
        "power_estimated": False, "window_kwh": 0, "peak_kwh": None, "daily": [],
    }
    r.update(over)
    return r


# ── the display rule ─────────────────────────────────────────────────────────

def test_blank_removes_a_site_split_estimate():
    rows = [_row(power_estimated=True)]
    fleet._blank_derived_power_on_dead_register(rows, {})
    assert rows[0]["current_power_w"] is None
    assert rows[0]["power_suppressed_reason"] == "site_split_estimate"


def test_blank_removes_a_borrowed_reading():
    """The Tannery #7 case exactly: the number came from another tenant."""
    borrow = {("sma", SERIAL): 8267.6}
    rows = [_row()]
    fleet._blank_derived_power_on_dead_register(rows, borrow)
    assert rows[0]["current_power_w"] is None
    assert rows[0]["power_suppressed_reason"] == "borrowed_from_another_tenant"


def test_blank_keeps_a_genuine_own_reading():
    """SMA's portal really does show 1,256 W for it — that we may show."""
    rows = [_row(current_power_w=1256.0, power_estimated=False)]
    fleet._blank_derived_power_on_dead_register(rows, {("sma", SERIAL): 8267.6})
    assert rows[0]["current_power_w"] == 1256.0
    assert "power_suppressed_reason" not in rows[0]


def test_blank_ignores_healthy_units():
    rows = [_row(no_energy_register=False, power_estimated=True)]
    fleet._blank_derived_power_on_dead_register(rows, {})
    assert rows[0]["current_power_w"] == 8267.6


def test_blank_uses_the_sn_key_not_serial():
    """Regression guard: the row key is 'sn'. Reading 'serial' fails SILENTLY.

    A mismatched key means the borrow never matches, nothing is suppressed, and
    the bug returns with no error anywhere.
    """
    rows = [_row()]
    fleet._blank_derived_power_on_dead_register(rows, {("sma", SERIAL): 8267.6})
    assert rows[0]["current_power_w"] is None, (
        "borrow lookup missed — is it keying on 'serial' instead of 'sn'?"
    )


def test_blank_survives_a_missing_borrow_map():
    rows = [_row(power_estimated=True)]
    fleet._blank_derived_power_on_dead_register(rows, None)
    assert rows[0]["current_power_w"] is None


# ── the borrow rule ──────────────────────────────────────────────────────────

def _tenant(tid: str, *, is_demo: bool) -> None:
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name=tid, contact_email=f"{tid}@t.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="comped", active=True, product="array_operator",
            is_demo=is_demo,
        ))
        db.commit()


def _inverter(tid: str, serial: str, watts: float) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, name="A-" + secrets.token_hex(3))
        db.add(a)
        db.flush()
        iv = Inverter(
            tenant_id=tid, array_id=a.id, vendor="sma", serial=serial,
            nameplate_kw=20.0, last_power_w=watts, last_power_at=now(),
            last_power_estimated=False,
        )
        db.add(iv)
        db.commit()
        return iv.id


def test_borrow_ignores_a_flagged_demo_tenant():
    ser = "SER" + secrets.token_hex(4)
    demo = "ten_" + secrets.token_hex(5)      # id gives NO hint it is a demo
    real = "ten_" + secrets.token_hex(5)
    _tenant(demo, is_demo=True)
    _tenant(real, is_demo=False)
    _inverter(demo, ser, 8267.6)
    mine = _inverter(real, ser, 100.0)
    with SessionLocal() as db:
        got = fleet._cross_tenant_live_by_serial(db, [db.get(Inverter, mine)])
    assert got.get(("sma", ser)) != 8267.6, "seeded demo watts leaked into a real fleet"


def test_borrow_still_works_between_real_tenants():
    """The feature must survive the fix — a real sibling may still lend upward."""
    ser = "SER" + secrets.token_hex(4)
    other, mine_t = "ten_" + secrets.token_hex(5), "ten_" + secrets.token_hex(5)
    _tenant(other, is_demo=False)
    _tenant(mine_t, is_demo=False)
    _inverter(other, ser, 4200.0)
    mine = _inverter(mine_t, ser, 10.0)
    with SessionLocal() as db:
        got = fleet._cross_tenant_live_by_serial(db, [db.get(Inverter, mine)])
    assert got.get(("sma", ser)) == 4200.0


def test_the_named_synthetic_tenants_are_excluded_by_id(monkeypatch):
    """A tenant on the SYNTHETIC list is excluded even with is_demo unset.

    ten_ford_demo_100 is the real-world instance — it carries 8267.6 W on SMA
    serial 191213319. This test does NOT create that tenant: doing so pollutes
    the shared test database and breaks test_fleet_credit_rate_excludes_demo,
    which asserts against it. Patch the list instead; the production code reads
    it at call time, so the mechanism under test is identical.
    """
    assert "ten_ford_demo_100" in SYNTHETIC_TENANT_IDS, (
        "the money path's synthetic list is what the borrow now reuses"
    )
    import api.rate_schedule as rs

    ser = "SER" + secrets.token_hex(4)
    seeded = "ten_" + secrets.token_hex(5)
    real = "ten_" + secrets.token_hex(5)
    _tenant(seeded, is_demo=False)      # flag deliberately NOT set
    _tenant(real, is_demo=False)
    monkeypatch.setattr(rs, "SYNTHETIC_TENANT_IDS", (seeded,))
    _inverter(seeded, ser, 8267.6)
    mine = _inverter(real, ser, 5.0)
    with SessionLocal() as db:
        got = fleet._cross_tenant_live_by_serial(db, [db.get(Inverter, mine)])
    assert got.get(("sma", ser)) != 8267.6


def test_stale_readings_are_still_excluded():
    ser = "SER" + secrets.token_hex(4)
    other, mine_t = "ten_" + secrets.token_hex(5), "ten_" + secrets.token_hex(5)
    _tenant(other, is_demo=False)
    _tenant(mine_t, is_demo=False)
    stale = _inverter(other, ser, 9000.0)
    with SessionLocal() as db:
        db.get(Inverter, stale).last_power_at = now() - timedelta(days=14)
        db.commit()
    mine = _inverter(mine_t, ser, 10.0)
    with SessionLocal() as db:
        got = fleet._cross_tenant_live_by_serial(db, [db.get(Inverter, mine)])
    assert got.get(("sma", ser)) != 9000.0
