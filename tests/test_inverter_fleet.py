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


if __name__ == "__main__":
    setup_module(None)
    test_reassign_changes_owner_group_not_source()
    test_create_array_and_cross_tenant_guard()
    test_peer_cohort_follows_owner_grouping()
    print("ALL PASS")
