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
    monkeypatch.setattr(wd, "send_internal_alert", lambda s, b: alerts.append((s, b)))
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
    monkeypatch.setattr(wd, "send_internal_alert", lambda s, b: alerts.append((s, b)))
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
    monkeypatch.setattr(wd, "send_internal_alert", lambda s, b: alerts.append((s, b)))
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
