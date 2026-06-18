"""Tests for the first-run Reports setup wizard endpoints."""
from __future__ import annotations

import secrets

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount


def _seed():
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Wiz Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="WC", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="Wiz Array", client_id=c.id, fuel_type="solar",
                    region="central"); db.add(arr); db.flush()
        db.add(UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="W1", enabled=True))
        db.commit()
        return tid, arr.id


def _auth(client, tid):
    # Mint a session for the tenant the same way other billing tests do.
    from api.account import mint_session_for_tenant
    return "Bearer " + mint_session_for_tenant(tid)


def test_setup_state_reports_arrays_and_no_customers(client):
    tid, aid = _seed()
    auth = _auth(client, tid)
    r = client.get("/v1/array-operator/billing/setup-state", headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["has_customers"] is False
    assert d["customer_count"] == 0
    assert len(d["arrays"]) == 1
    a = d["arrays"][0]
    assert a["name"] == "Wiz Array"
    assert a["provider"] == "gmp"
    assert a["age_known"] is False           # no install date yet
    assert a["auto_net_rate"] > 0            # resolver always returns a rate
    assert "default" in d["global"] or "effective_discount_pct" in d["global"]


def test_set_array_age_then_state_reflects_it(client):
    tid, aid = _seed()
    auth = _auth(client, tid)
    # Set install year via the wizard endpoint.
    r = client.patch(f"/v1/array-operator/billing/arrays/{aid}",
                     json={"install_year": 2018},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["first_connect_date"] == "2018-01-01"
    # setup-state now shows the age as known.
    d = client.get("/v1/array-operator/billing/setup-state",
                   headers={"Authorization": auth}).json()
    a = d["arrays"][0]
    assert a["age_known"] is True
    assert a["install_year"] == 2018
    assert a["age_bucket"] in ("le11", "gt11")


def test_set_array_age_rejects_bad_year_and_foreign_array(client):
    tid, aid = _seed()
    auth = _auth(client, tid)
    r = client.patch(f"/v1/array-operator/billing/arrays/{aid}",
                     json={"install_year": 1800}, headers={"Authorization": auth})
    assert r.status_code == 400
    # Another tenant's array → 404
    tid2, aid2 = _seed()
    r = client.patch(f"/v1/array-operator/billing/arrays/{aid2}",
                     json={"install_year": 2020}, headers={"Authorization": auth})
    assert r.status_code == 404
