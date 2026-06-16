"""Test the owner-arrangeable inverter fleet: persist, regroup, peer-cohort, reset."""
import os, tempfile
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
import pytest
from api.db import SessionLocal, init_db
from api.models import Tenant, Array, Inverter
from api import inverter_fleet as IF


def _mk_tenant(db, key="ten_fleet_test"):
    import secrets
    t = Tenant(id="ten_" + secrets.token_hex(6), name="Fleet Test",
               contact_email="t@example.com", tenant_key="sol_live_" + secrets.token_hex(6),
               product="array_operator", active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def setup_module(m):
    init_db()


def test_reassign_changes_owner_group_not_source():
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a1 = Array(tenant_id=t.id, name="Londonderry", fuel_type="solar")
        a2 = Array(tenant_id=t.id, name="South Barn", fuel_type="solar")
        db.add_all([a1, a2]); db.commit(); db.refresh(a1); db.refresh(a2)
        iv = Inverter(tenant_id=t.id, array_id=a1.id, position=1, vendor="solaredge",
                      serial="SN-1", source_site_id="416160", source_array_id=a1.id,
                      nameplate_kw=33.3, name="Inverter 1")
        db.add(iv); db.commit(); db.refresh(iv)

        # move it to a2
        moved = IF.reassign_inverter(db, t, iv.id, a2.id)
        assert moved.array_id == a2.id           # owner group changed
        assert moved.source_array_id == a1.id    # source UNCHANGED
        assert moved.source_site_id == "416160"  # telemetry origin UNCHANGED

        # reset snaps back to source
        n = IF.reset_layout(db, t)
        assert n == 1
        db.refresh(iv)
        assert iv.array_id == a1.id
        print("PASS reassign+reset")


def test_create_array_and_cross_tenant_guard():
    with SessionLocal() as db:
        t = _mk_tenant(db)
        other = _mk_tenant(db)
        a = Array(tenant_id=t.id, name="A", fuel_type="solar")
        db.add(a); db.commit(); db.refresh(a)
        iv = Inverter(tenant_id=t.id, array_id=a.id, position=1, vendor="solaredge",
                      serial="SN-X", source_array_id=a.id)
        db.add(iv); db.commit(); db.refresh(iv)

        newarr = IF.create_array(db, t, "My South Roof")
        assert newarr.tenant_id == t.id and newarr.name == "My South Roof"

        # cross-tenant move must fail
        with pytest.raises(IF.FleetError):
            IF.reassign_inverter(db, other, iv.id, newarr.id)
        print("PASS create+guard")


def test_peer_cohort_follows_owner_grouping():
    """Two inverters together = peer signal; split apart = degenerate (no peers)."""
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a1 = Array(tenant_id=t.id, name="Group", fuel_type="solar")
        db.add(a1); db.commit(); db.refresh(a1)
        # build_fleet_tree pulls telemetry (none here, no real creds) — just assert
        # the grouping/cohort plumbing runs and groups by owner array_id.
        for i in range(2):
            db.add(Inverter(tenant_id=t.id, array_id=a1.id, position=i+1,
                            vendor="solaredge", serial=f"SN-{i}", source_array_id=a1.id,
                            nameplate_kw=20.0, name=f"Inv {i}"))
        db.commit()
        tree = IF.build_fleet_tree(db, t)
        col = next(c for c in tree["columns"] if c["array_id"] == a1.id)
        assert col["inverter_count"] == 2
        assert {iv["sn"] for iv in col["inverters"]} == {"SN-0", "SN-1"}
        print("PASS cohort grouping")


def test_origin_links_deep_link_to_vendor_portal():
    """Owners click an array/inverter to jump to the vendor's origin site."""
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a = Array(tenant_id=t.id, name="Origin Roof", fuel_type="solar")
        db.add(a); db.commit(); db.refresh(a)
        # SolarEdge inverter with a known site -> site-specific deep link.
        se = Inverter(tenant_id=t.id, array_id=a.id, position=1, vendor="solaredge",
                      serial="SN-SE", source_site_id="416160", source_array_id=a.id,
                      nameplate_kw=33.3, name="SE 1")
        # SMA inverter with no site -> vendor base URL (not None).
        sma = Inverter(tenant_id=t.id, array_id=a.id, position=2, vendor="sma",
                       serial="SN-SMA", source_site_id=None, source_array_id=a.id,
                       nameplate_kw=20.0, name="SMA 1")
        db.add_all([se, sma]); db.commit()

        tree = IF.build_fleet_tree(db, t)
        col = next(c for c in tree["columns"] if c["array_id"] == a.id)
        rows = {iv["sn"]: iv for iv in col["inverters"]}

        assert rows["SN-SE"]["origin_url"] == \
            "https://monitoring.solaredge.com/solaredge-web/p/site/416160/#/dashboard"
        assert rows["SN-SE"]["origin_label"] == "SolarEdge"
        # SMA: key-less vendor falls back to the base URL, never None.
        assert rows["SN-SMA"]["origin_url"] == "https://ennexos.sunnyportal.com/"

        # Distinct origin links on the column, deduped by (vendor, site_id).
        links = col["origin_links"]
        se_links = [l for l in links if l["url"].startswith("https://monitoring.solaredge.com")]
        assert len(se_links) == 1
        assert se_links[0]["url"] == \
            "https://monitoring.solaredge.com/solaredge-web/p/site/416160/#/dashboard"
        print("PASS origin links")


def test_inverter_daily_series_and_min_peak(monkeypatch):
    """Each inverter row carries a real daily kWh series + min/peak derived from it."""
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a = Array(tenant_id=t.id, name="Telemetry Roof", fuel_type="solar")
        db.add(a); db.commit(); db.refresh(a)
        iv = Inverter(tenant_id=t.id, array_id=a.id, position=1, vendor="solaredge",
                      serial="SN-D", source_site_id="416160", source_array_id=a.id,
                      nameplate_kw=10.0, name="Daily 1")
        db.add(iv); db.commit()

        # Stub the per-site telemetry so we feed a known daily series (no real creds).
        series = [
            {"date": "2026-06-09", "kwh": 41.2},
            {"date": "2026-06-10", "kwh": 8.5},      # the min
            {"date": "2026-06-11", "kwh": 52.7},     # the peak
            {"date": "2026-06-12", "kwh": 47.0},
        ]
        def _fake_tel(vendor, api_key, site_id, *, force=False):
            return {"SN-D": {"name": "Daily 1", "model": "SE10K", "nameplate_kw": 10.0,
                             "daily": series, "error_code": None,
                             "last_report": "2026-06-12T12:00:00",
                             "last_mode": "PRODUCING", "last_power_w": 6400}}
        monkeypatch.setattr(IF, "_telemetry_for_site", _fake_tel)
        # ensure a resolvable connection so telemetry is pulled
        a.solaredge_api_key = "fake_key"; a.solaredge_site_id = 416160; db.commit()

        tree = IF.build_fleet_tree(db, t, force_refresh=True)
        col = next(c for c in tree["columns"] if c["array_id"] == a.id)
        row = next(iv for iv in col["inverters"] if iv["sn"] == "SN-D")

        assert [d["kwh"] for d in row["daily"]] == [41.2, 8.5, 52.7, 47.0]
        assert row["min_kwh"] == 8.5
        assert row["peak_kwh"] == 52.7
        assert row["current_power_w"] == 6400
        print("PASS daily series + min/peak")


def test_persist_on_read_survives_api_outage(monkeypatch):
    """Live daily readings are snapshotted into InverterDaily on read, so the graph
    keeps its history even when the API later returns nothing (the SolarEdge case)."""
    from api.models import InverterDaily
    from sqlalchemy import select as _select, func as _func
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a = Array(tenant_id=t.id, name="Persist Roof", fuel_type="solar")
        db.add(a); db.commit(); db.refresh(a)
        iv = Inverter(tenant_id=t.id, array_id=a.id, position=1, vendor="solaredge",
                      serial="SN-P", source_site_id="416160", source_array_id=a.id,
                      nameplate_kw=10.0, name="Persist 1")
        db.add(iv); db.commit(); db.refresh(iv)
        a.solaredge_api_key = "fake_key"; a.solaredge_site_id = 416160; db.commit()
        inv_id = iv.id

        series = [
            {"date": "2026-06-10", "kwh": 40.0},
            {"date": "2026-06-11", "kwh": 50.0},
            {"date": "2026-06-12", "kwh": 45.0},
        ]
        # FIRST read: API returns the series → should persist into InverterDaily
        def _tel_live(vendor, api_key, site_id, *, force=False):
            return {"SN-P": {"name": "Persist 1", "model": "SE10K", "nameplate_kw": 10.0,
                             "daily": series, "error_code": None,
                             "last_report": "2026-06-12T12:00:00",
                             "last_mode": "PRODUCING", "last_power_w": 6000}}
        monkeypatch.setattr(IF, "_telemetry_for_site", _tel_live)
        tree1 = IF.build_fleet_tree(db, t, force_refresh=True)
        row1 = next(r for c in tree1["columns"] for r in c["inverters"] if r["sn"] == "SN-P")
        assert len(row1["daily"]) == 3

        # storage now holds the 3 days
        with SessionLocal() as db2:
            stored = db2.execute(_select(_func.count()).select_from(InverterDaily)
                                 .where(InverterDaily.inverter_id == inv_id)).scalar()
            assert stored == 3, f"expected 3 persisted days, got {stored}"

        # SECOND read: API now returns NOTHING (outage / off-peak) → graph must STILL
        # have its history from storage. This is the whole point.
        def _tel_dead(vendor, api_key, site_id, *, force=False):
            return {}
        monkeypatch.setattr(IF, "_telemetry_for_site", _tel_dead)
        tree2 = IF.build_fleet_tree(db, t, force_refresh=True)
        row2 = next(r for c in tree2["columns"] for r in c["inverters"] if r["sn"] == "SN-P")
        assert len(row2["daily"]) == 3, "graph history vanished on API outage — store failed"
        assert row2["min_kwh"] == 40.0 and row2["peak_kwh"] == 50.0
        print("PASS persist-on-read survives API outage")


def test_persist_keeps_larger_kwh_on_reread(monkeypatch):
    """A day's energy only climbs — re-seeing a smaller (cached/partial) value must
    NOT clobber a fuller one already stored."""
    from api.models import InverterDaily
    from sqlalchemy import select as _select
    with SessionLocal() as db:
        t = _mk_tenant(db)
        a = Array(tenant_id=t.id, name="Climb Roof", fuel_type="solar")
        db.add(a); db.commit(); db.refresh(a)
        iv = Inverter(tenant_id=t.id, array_id=a.id, position=1, vendor="solaredge",
                      serial="SN-C", source_site_id="416160", source_array_id=a.id,
                      nameplate_kw=10.0, name="Climb 1")
        db.add(iv); db.commit(); db.refresh(iv)
        a.solaredge_api_key = "fake_key"; a.solaredge_site_id = 416160; db.commit()
        inv_id = iv.id

        def mk(kwh):
            def _tel(vendor, api_key, site_id, *, force=False):
                return {"SN-C": {"name": "Climb 1", "nameplate_kw": 10.0,
                                 "daily": [{"date": "2026-06-12", "kwh": kwh}],
                                 "error_code": None, "last_report": None,
                                 "last_mode": "PRODUCING", "last_power_w": 5000}}
            return _tel
        monkeypatch.setattr(IF, "_telemetry_for_site", mk(55.0)); IF.build_fleet_tree(db, t, force_refresh=True)
        monkeypatch.setattr(IF, "_telemetry_for_site", mk(20.0)); IF.build_fleet_tree(db, t, force_refresh=True)
        with SessionLocal() as db2:
            row = db2.execute(_select(InverterDaily).where(
                InverterDaily.inverter_id == inv_id)).scalars().one()
            assert row.kwh == 55.0, f"smaller re-read clobbered stored value: {row.kwh}"
        print("PASS persist keeps larger kwh")


if __name__ == "__main__":
    setup_module(None)
    test_reassign_changes_owner_group_not_source()
    test_create_array_and_cross_tenant_guard()
    test_peer_cohort_follows_owner_grouping()
    test_origin_links_deep_link_to_vendor_portal()
    print("ALL PASS")
