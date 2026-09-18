"""Tests for the billing-safety generation watchdog (api/jobs/generation_watchdog).

The watchdog is belt-and-suspenders behind the ingest plausibility guard: it
scans DailyGeneration (billing meter) + InverterDaily for physically impossible
kWh values (> nameplate × 24h) and alerts. These tests prove it FLAGS bad data
and stays SILENT on clean data.
"""
import datetime as dt
import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, DailyGeneration, Inverter, InverterDaily, Tenant
import api.jobs.generation_watchdog as wd


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="WD Test", contact_email=f"{tid}@t.test",
                      tenant_key="k_" + secrets.token_hex(8), plan="standard",
                      active=True, product="array_operator"))
        db.commit()
    return tid


def _array_with_inverter(tid: str, nameplate_kw: float) -> tuple[int, int]:
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="WD Array", fuel_type="solar")
        db.add(arr); db.flush()
        iv = Inverter(tenant_id=tid, array_id=arr.id, vendor="fronius",
                      serial="wd-" + secrets.token_hex(4), nameplate_kw=nameplate_kw)
        db.add(iv); db.commit()
        return arr.id, iv.id


def test_watchdog_silent_when_clean(monkeypatch):
    alerts = []
    monkeypatch.setattr(wd, "send_internal_alert",
                        lambda s, b, **k: alerts.append((s, b)))
    tid = _tenant()
    aid, ivid = _array_with_inverter(tid, 7.6)   # ceiling 182.4 kWh/day
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid,
                               day=dt.date(2026, 6, 13), kwh=40.0, source="x"))
        db.add(InverterDaily(tenant_id=tid, inverter_id=ivid,
                             day=dt.date(2026, 6, 13), kwh=38.0, source="x"))
        db.commit()
    result = wd.run_generation_watchdog()
    assert result["ok"] is True
    assert alerts == []                          # silent on clean data


def test_watchdog_flags_impossible_daily_generation(monkeypatch):
    alerts = []
    monkeypatch.setattr(wd, "send_internal_alert",
                        lambda s, b, **k: alerts.append((s, b)))
    tid = _tenant()
    aid, ivid = _array_with_inverter(tid, 144.0)  # array ceiling 3,456 kWh/day
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid,
                               day=dt.date(2026, 6, 14), kwh=677533.0, source="x"))
        db.commit()
    result = wd.run_generation_watchdog()
    assert result["ok"] is False
    assert len(result["daily"]) == 1
    assert result["daily"][0]["kwh"] == 677533.0
    assert len(alerts) == 1                       # alerted exactly once
    assert "implausible" in alerts[0][0].lower() or "implausible" in alerts[0][1].lower()


def test_watchdog_flags_impossible_inverter_daily(monkeypatch):
    alerts = []
    monkeypatch.setattr(wd, "send_internal_alert",
                        lambda s, b, **k: alerts.append((s, b)))
    tid = _tenant()
    aid, ivid = _array_with_inverter(tid, 7.6)    # inverter ceiling 182.4 kWh/day
    with SessionLocal() as db:
        db.add(InverterDaily(tenant_id=tid, inverter_id=ivid,
                             day=dt.date(2026, 6, 14), kwh=36411.0, source="x"))
        db.commit()
    result = wd.run_generation_watchdog()
    assert result["ok"] is False
    assert len(result["inverter"]) == 1
    assert result["inverter"][0]["kwh"] == 36411.0
    assert len(alerts) == 1


def test_watchdog_ignores_deleted_and_inactive_tenants(monkeypatch):
    """A tenant that can no longer be invoiced cannot be over-invoiced.

    hard_delete scrubs a removed tenant's contact to deleted+ten_xxx@invalid.local
    but does NOT soft-delete its arrays, so its junk rows kept surfacing in the
    daily alert forever, naming accounts that will never bill again (Ford,
    2026-09-18 - 37 of these in the ops inbox).

    Other tests in this module leave their own bad rows behind, so assert on the
    ABSENCE of these two tenants rather than on a globally clean scan.
    """
    monkeypatch.setattr(wd, "send_internal_alert", lambda s, b, **k: None)

    # 1. Deactivated tenant with an impossible row.
    dead = _tenant()
    aid, _ = _array_with_inverter(dead, 144.0)   # ceiling 3,456 kWh/day
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=dead, array_id=aid,
                               day=dt.date(2026, 6, 14), kwh=677533.0, source="x"))
        db.get(Tenant, dead).active = False
        db.commit()

    # 2. Hard-deleted tenant: contact scrubbed to the invalid.local sentinel.
    scrubbed = _tenant()
    aid2, _ = _array_with_inverter(scrubbed, 144.0)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=scrubbed, array_id=aid2,
                               day=dt.date(2026, 6, 15), kwh=677533.0, source="x"))
        db.get(Tenant, scrubbed).contact_email = f"deleted+{scrubbed}@invalid.local"
        db.commit()

    # 3. Control: a live tenant with the same junk MUST still be reported.
    live = _tenant()
    aid3, _ = _array_with_inverter(live, 144.0)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=live, array_id=aid3,
                               day=dt.date(2026, 6, 17), kwh=677533.0, source="x"))
        db.commit()

    result = wd.scan_implausible_generation()
    reported_arrays = {b["array_id"] for b in result["daily"]}
    assert aid not in reported_arrays, "deactivated tenant still reported"
    assert aid2 not in reported_arrays, "hard-deleted tenant still reported"
    assert aid3 in reported_arrays, "live tenant must still be reported"
    assert not any("invalid.local" in str(b["tenant"]) for b in result["daily"])


def test_watchdog_repeat_alert_is_throttled_not_daily(monkeypatch):
    """The finding set is stable until someone runs the correction sweep, so the
    alert must carry a long throttle window rather than re-mailing every day."""
    captured = {}
    monkeypatch.setattr(
        wd, "send_internal_alert",
        lambda s, b, **k: captured.update(subject=s, throttle_min=k.get("throttle_min")))
    tid = _tenant()
    aid, _ = _array_with_inverter(tid, 144.0)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=aid,
                               day=dt.date(2026, 6, 16), kwh=677533.0, source="x"))
        db.commit()

    wd.run_generation_watchdog()
    assert "implausible" in captured["subject"]
    assert captured["throttle_min"] == 60 * 24 * 7
