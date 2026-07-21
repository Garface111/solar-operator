"""Full-history tools: production_history, tenant_data_catalog, underperformer name match."""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy import select

from api.db import SessionLocal
from api.models import (
    Array,
    Bill,
    DailyGeneration,
    Inverter,
    InverterDaily,
    Tenant,
    UtilityAccount,
)
from api import ea_ops_tools as eot
from api import energy_agent as ea


def _tenant() -> tuple[str, Tenant]:
    tid = "ten_" + secrets.token_hex(5)
    key = "sol_" + secrets.token_hex(6)
    with SessionLocal() as db:
        t = Tenant(
            id=tid,
            name="History Owner",
            contact_email=f"{key}@owner.test",
            tenant_key=key,
            plan="comped",
            active=True,
            product="array_operator",
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return tid, t


def _seed_tannery(tid: str) -> tuple[int, int]:
    """Return (array_id, low_inverter_id) with 90d of peer-skewed daily data + bills."""
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="Tannery Brook 140kW")
        db.add(arr)
        db.flush()
        invs = []
        for i, sn in enumerate(["191213487", "191213488", "191213489", "191213490"]):
            inv = Inverter(
                tenant_id=tid,
                array_id=arr.id,
                vendor="sma",
                serial=sn,
                name=f"#{i + 1}",
                nameplate_kw=35.0,
                position=i,
            )
            db.add(inv)
            db.flush()
            invs.append(inv)
        today = date.today()
        for d in range(90):
            day = today - timedelta(days=89 - d)
            for j, inv in enumerate(invs):
                kwh = 80.0 if j == 0 else 100.0
                kwh += (d % 5) * 0.2
                db.add(
                    InverterDaily(
                        tenant_id=tid,
                        inverter_id=inv.id,
                        day=day,
                        kwh=kwh,
                        source="test",
                    )
                )
            db.add(
                DailyGeneration(
                    tenant_id=tid,
                    array_id=arr.id,
                    day=day,
                    kwh=380.0,
                    source="test",
                )
            )
        ua = UtilityAccount(
            tenant_id=tid,
            provider="gmp",
            account_number="TB-1",
            nickname="Tannery",
            array_id=arr.id,
        )
        db.add(ua)
        db.flush()
        for m in range(6):
            pe = today - timedelta(days=30 * m)
            db.add(
                Bill(
                    tenant_id=tid,
                    account_id=ua.id,
                    period_end=pe,
                    period_start=pe - timedelta(days=30),
                    kwh_generated=11000,
                    kwh_sent_to_grid=9000,
                    solar_credit_usd=1800.0,
                )
            )
        db.commit()
        return arr.id, invs[0].id


def test_underperformer_history_matches_array_name_key(monkeypatch):
    """Fleet tree uses array_name — old code looked at col['name'] and returned empty."""
    tid, _ = _tenant()
    aid, inv0 = _seed_tannery(tid)

    def fake_cols(db, tenant):
        return (
            [
                {
                    "array_id": aid,
                    "array_name": "Tannery Brook 140kW",  # NOT "name"
                    "inverters": [
                        {
                            "inverter_id": inv0,
                            "id": inv0,
                            "name": "#1",
                            "serial": "191213487",
                            "status": "underperforming",
                            "peer_index": 0.82,
                            "nameplate_kw": 35,
                        }
                    ],
                }
            ],
            {},
        )

    monkeypatch.setattr(ea, "_fleet_tree_columns", fake_cols)

    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        out = eot.underperformer_history(
            db,
            tenant,
            args={
                "array_name": "Tannery Brook",
                "inverter_id": inv0,
                "window_days": 180,
            },
        )
    assert out["ok"] is True
    assert len(out["units"]) == 1
    u = out["units"][0]
    assert u["days_of_history"] >= 60
    assert u["series_source"] == "inverter_daily"
    assert u["array_name"] == "Tannery Brook 140kW"


def test_production_history_full_series_peers_bills():
    tid, _ = _tenant()
    aid, inv0 = _seed_tannery(tid)
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        out = eot.production_history(
            db, tenant, args={"inverter_id": inv0, "window_days": 0}
        )
    assert out["ok"] is True
    assert out["subject"]["days_of_history"] >= 80
    assert out["subject"]["series_source"] == "inverter_daily"
    assert len(out["peers_on_array"]) >= 2
    assert out["subject_vs_peer_avg_ratio"] is not None
    # low unit should be well below peers (~0.8)
    assert out["subject_vs_peer_avg_ratio"] < 0.95
    assert out["bills"]["count"] >= 1
    assert "judgment_guardrail" in out
    assert out["array"]["id"] == aid


def test_tenant_data_catalog_and_query_all_history():
    tid, _ = _tenant()
    aid, inv0 = _seed_tannery(tid)
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        cat = eot.tenant_data_catalog(db, tenant, args={})
        assert cat["streams"]["inverter_daily"]["rows"] >= 90
        assert cat["streams"]["bills"]["count"] >= 1
        assert any(p["array_id"] == aid for p in cat["per_array"])

        q = ea._query_tenant_tool(
            db,
            tenant,
            {
                "resource": "inverter_daily",
                "inverter_id": inv0,
                "days": 0,
                "limit": 500,
            },
        )
        assert q["count"] > 50
        assert q["coverage_all_time"]["total_rows_all_time"] >= 90

        qb = ea._query_tenant_tool(
            db, tenant, {"resource": "bills", "days": 0, "array_id": aid}
        )
        assert qb["count"] >= 1
        assert qb["coverage_all_time"]["total_bills"] >= 1

        qm = ea._query_tenant_tool(
            db,
            tenant,
            {
                "resource": "daily_generation",
                "array_id": aid,
                "days": 0,
                "group_by": "month",
            },
        )
        assert len(qm.get("rows") or []) >= 1


def test_tools_registered_in_run_tool():
    names = {t["function"]["name"] for t in ea.TOOL_DEFS}
    assert "production_history" in names
    assert "tenant_data_catalog" in names
    assert "underperformer_history" in names
