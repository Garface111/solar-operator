"""A vendor's capture must NOT merge into an existing array of a DIFFERENT vendor
(the bug that buried Ford's CHINT inverters inside his SolarEdge 'Londonderry')."""
import secrets

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant, Array, Inverter


def _seed_mixed_array():
    tid = "ten_" + secrets.token_hex(6)
    key = "inv_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Chint Test", contact_email=f"{key}@t.test",
                      tenant_key=key, plan="standard", active=True))
        db.flush()
        arr = Array(tenant_id=tid, name="Londonderry", fuel_type="solar")
        db.add(arr); db.flush()
        aid = arr.id
        for i, s in enumerate(["S1", "S2"]):
            db.add(Inverter(tenant_id=tid, array_id=aid, vendor="solaredge", serial=s,
                            position=i, source_array_id=aid))
        for i, s in enumerate(["C1", "C2"]):  # chint wrongly merged into the SE array
            db.add(Inverter(tenant_id=tid, array_id=aid, vendor="chint", serial=s,
                            position=i + 2, source_array_id=aid, source_site_id="site_x"))
        db.commit()
    return tid, key, aid


def test_chint_capture_unmerges_from_solaredge_array(client):
    tid, key, aid = _seed_mixed_array()
    r = client.post(
        "/v1/array-owners/inverter-capture",
        headers={"Authorization": f"Bearer {key}"},
        json={"provider": "chint", "sites": [{
            "site_id": "site_x", "name": "Londonderry",
            "inverters": [{"serial": "C1"}, {"serial": "C2"}],
        }]},
    )
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        invs = db.execute(select(Inverter).where(
            Inverter.tenant_id == tid, Inverter.deleted_at.is_(None))).scalars().all()
        name_of = {a.id: a.name for a in db.execute(
            select(Array).where(Array.tenant_id == tid)).scalars().all()}
        chint_arrays = {name_of[iv.array_id] for iv in invs if iv.vendor == "chint"}
        se_arrays = {name_of[iv.array_id] for iv in invs if iv.vendor == "solaredge"}
        chint_aids = {iv.array_id for iv in invs if iv.vendor == "chint"}
        se_aids = {iv.array_id for iv in invs if iv.vendor == "solaredge"}
    assert chint_arrays == {"Londonderry (Chint)"}, f"chint should split out, got {chint_arrays}"
    assert se_arrays == {"Londonderry"}, f"solaredge should stay put, got {se_arrays}"
    assert chint_aids.isdisjoint(se_aids), "no array may hold both vendors after the split"
