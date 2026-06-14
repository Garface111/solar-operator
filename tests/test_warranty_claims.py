"""Automatic warranty claims — engine + API tests.

Covers the server-authoritative claim engine in api/warranty_claims.py:
  - reconcile OPENS a claim for each dead/fault inverter (and ONLY those)
  - evidence is peer-measured + the draft is built
  - send policy: manual (ready) / auto (filed) / delay (queued → due → filed)
  - reconcile CLOSES claims when the inverter recovers (sent→resolved banks $,
    ready→cleared catches a blip)
  - the full lifecycle API: list / settings / send / resolve / dismiss / reopen /
    per-claim mode / draft edits

The fleet tree is INJECTED (reconcile accepts `tree=`), so no SolarEdge calls.
Email send is monkeypatched — nothing leaves the box.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api.warranty_claims as wc
from api.db import SessionLocal
from api.models import Array, Inverter, Tenant, WarrantyClaim, now


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_tenant(**over) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    fields = dict(
        id=tid, name="Owner Test", contact_email=f"{key}@owner.test",
        tenant_key=key, plan="comped", active=True, product="array_operator",
    )
    fields.update(over)
    with SessionLocal() as db:
        db.add(Tenant(**fields))
        db.commit()
    return tid, key


def _array(tenant_id: str, name: str) -> int:
    with SessionLocal() as db:
        arr = Array(tenant_id=tenant_id, name=name)
        db.add(arr)
        db.flush()
        aid = arr.id
        db.commit()
    return aid


def _persist_inv(tid, aid, *, name="Inverter 1", np=10.0) -> tuple[int, str]:
    """A real persisted Inverter row (FK target for the claim). In production the
    fleet tree always references persisted inverters, so tests do too."""
    with SessionLocal() as db:
        iv = Inverter(tenant_id=tid, array_id=aid, vendor="solaredge",
                      serial="SN-" + secrets.token_hex(3), position=0,
                      name=name, model=f"SE{np}K", nameplate_kw=np)
        db.add(iv)
        db.flush()
        rid, sn = iv.id, iv.serial
        db.commit()
    return rid, sn


def _inv(inv_id, status, *, sn=None, name="Inverter 1", np=10.0, window_kwh=None,
         pi=None, stale=None):
    """One inverter row in the shape build_fleet_tree emits."""
    if window_kwh is None:
        window_kwh = 0.0 if status in ("dead", "fault") else np * 4.6 * wc.WINDOW_DAYS
    return {
        "inverter_id": inv_id, "sn": sn or f"SN-{inv_id}", "name": name,
        "model": f"SE{np}K", "vendor": "solaredge", "nameplate_kw": np,
        "peer_index": pi, "status": status, "window_kwh": window_kwh,
        "stale_hours": stale,
    }


def _tree(array_id, array_name, inverters):
    return {"columns": [{
        "array_id": array_id, "array_name": array_name,
        "vendor": "solaredge", "inverters": inverters,
    }]}


def _tenant_obj(tid):
    with SessionLocal() as db:
        return db.get(Tenant, tid)


def _claims(tid):
    with SessionLocal() as db:
        return db.execute(
            select(WarrantyClaim).where(WarrantyClaim.tenant_id == tid)
            .order_by(WarrantyClaim.id)
        ).scalars().all()


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    """Pretend every send succeeds, but never touch Resend."""
    sent = []
    def fake(to, subject, body_text, **kw):
        sent.append({"to": to, "subject": subject, "body": body_text, **kw})
        return True
    monkeypatch.setattr(wc.notify, "send_warranty_claim_email", fake)
    return sent


# ── reconcile: open ───────────────────────────────────────────────────────────

def test_reconcile_opens_one_claim_per_warrantable_failure():
    tid, _ = _make_tenant()              # default policy = manual
    aid = _array(tid, "Londonderry")
    dead_id, dead_sn = _persist_inv(tid, aid, name="Inv A")
    fault_id, fault_sn = _persist_inv(tid, aid, name="Inv B")
    tree = _tree(aid, "Londonderry", [
        _inv(dead_id, "dead", sn=dead_sn, name="Inv A", stale=72),
        _inv(fault_id, "fault", sn=fault_sn, name="Inv B", pi=0.2),
        _inv(103, "underperforming", name="Inv C", pi=0.6),
        _inv(104, "comm_gap", name="Inv D", stale=30),
        _inv(105, "ok", name="Inv E"),
        _inv(106, "ok", name="Inv F"),
    ])
    with SessionLocal() as db:
        tally = wc.reconcile(db, db.get(Tenant, tid), tree=tree)

    assert tally["opened"] == 2 and tally["closed"] == 0
    claims = _claims(tid)
    assert {c.fail_type for c in claims} == {"dead", "fault"}
    assert all(c.stage == "ready" for c in claims)          # manual default
    # evidence + draft populated
    dead = next(c for c in claims if c.fail_type == "dead")
    assert dead.evidence["lostKwh"] > 0
    assert dead.evidence["lostYr"] > dead.evidence["lostMo"]
    assert dead.evidence["daysDown"] == 3                    # 72h
    assert "warranty claim" in dead.draft["body"].lower()
    assert dead.draft["to"] == "support@solaredge.com"


def test_reconcile_is_idempotent_no_duplicate_claims():
    tid, _ = _make_tenant()
    aid = _array(tid, "Stowe")
    iid, sn = _persist_inv(tid, aid)
    tree = _tree(aid, "Stowe", [_inv(iid, "dead", sn=sn)])
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=tree)
    with SessionLocal() as db:
        tally = wc.reconcile(db, db.get(Tenant, tid), tree=tree)
    assert tally["opened"] == 0
    assert len(_claims(tid)) == 1


# ── reconcile: close ──────────────────────────────────────────────────────────

def test_recovery_after_filing_resolves_and_banks_value():
    tid, _ = _make_tenant(claim_send_mode="auto")
    aid = _array(tid, "Waitsfield")
    iid, sn = _persist_inv(tid, aid, np=20.0)
    fail = _tree(aid, "Waitsfield", [_inv(iid, "dead", sn=sn, np=20.0, stale=96)])
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=fail)
    claim = _claims(tid)[0]
    assert claim.stage == "sent"                            # auto-filed

    # inverter healthy again next reconcile → resolved + recovered banked
    healed = _tree(aid, "Waitsfield", [_inv(iid, "ok", sn=sn, np=20.0)])
    with SessionLocal() as db:
        tally = wc.reconcile(db, db.get(Tenant, tid), tree=healed)
    assert tally["closed"] == 1
    claim = _claims(tid)[0]
    assert claim.stage == "resolved" and claim.auto_resolved is True
    assert claim.recovered_usd == round(claim.evidence["lostYr"])


def test_recovery_before_filing_is_cleared_not_resolved():
    tid, _ = _make_tenant()                                 # manual → stays ready
    aid = _array(tid, "Bristol")
    iid, sn = _persist_inv(tid, aid)
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=_tree(aid, "Bristol", [_inv(iid, "fault", sn=sn)]))
    assert _claims(tid)[0].stage == "ready"
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=_tree(aid, "Bristol", [_inv(iid, "ok", sn=sn)]))
    claim = _claims(tid)[0]
    assert claim.stage == "cleared" and claim.recovered_usd == 0


# ── send policy ───────────────────────────────────────────────────────────────

def test_auto_policy_files_immediately(_no_real_email):
    tid, _ = _make_tenant(claim_send_mode="auto")
    aid = _array(tid, "Cabot")
    iid, sn = _persist_inv(tid, aid)
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=_tree(aid, "Cabot", [_inv(iid, "dead", sn=sn)]))
    claim = _claims(tid)[0]
    assert claim.stage == "sent" and claim.sent_via == "auto"
    assert len(_no_real_email) == 1


def test_delay_policy_queues_then_fires_when_due(_no_real_email):
    tid, _ = _make_tenant(claim_send_mode="delay", claim_grace_hours=24)
    aid = _array(tid, "Hardwick")
    iid, sn = _persist_inv(tid, aid)
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=_tree(aid, "Hardwick", [_inv(iid, "dead", sn=sn)]))
    claim = _claims(tid)[0]
    assert claim.stage == "queued" and claim.send_at is not None

    # not due yet → nothing fires
    with SessionLocal() as db:
        assert wc.process_due(db, db.get(Tenant, tid)) == 0

    # backdate the timer → due → fires
    with SessionLocal() as db:
        c = db.get(WarrantyClaim, claim.id)
        c.send_at = now() - timedelta(minutes=1)
        db.commit()
    with SessionLocal() as db:
        n = wc.process_due(db, db.get(Tenant, tid))
    assert n == 1 and _claims(tid)[0].stage == "sent"


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    from api.app import app
    return TestClient(app)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _seed(tid, inverters, name="Site"):
    aid = _array(tid, name)
    for row in inverters:                       # claimed inverters need real FK targets
        if row["status"] in ("dead", "fault"):
            rid, sn = _persist_inv(tid, aid, name=row["name"], np=row["nameplate_kw"])
            row["inverter_id"], row["sn"] = rid, sn
    with SessionLocal() as db:
        wc.reconcile(db, db.get(Tenant, tid), tree=_tree(aid, name, inverters))
    return aid


def test_list_endpoint_returns_ledger_settings_summary(client):
    tid, key = _make_tenant()
    _seed(tid, [_inv(701, "dead"), _inv(702, "fault"), _inv(703, "ok")])
    r = client.get("/v1/array-owners/claims?reconcile_first=0", headers=_auth(key))
    assert r.status_code == 200
    data = r.json()
    assert len(data["claims"]) == 2
    assert data["settings"]["sendMode"] == "manual"
    assert data["settings"]["graceHours"] == 24
    assert data["summary"]["open"] == 2 and data["summary"]["awaiting"] == 2
    assert data["summary"]["atStakeYr"] > 0


def test_settings_endpoint_changes_policy_and_replaces_pending(client):
    tid, key = _make_tenant()
    _seed(tid, [_inv(801, "dead")])
    # switch to auto-send + grace
    r = client.post("/v1/array-owners/claims/settings",
                    json={"sendMode": "delay", "graceHours": 6}, headers=_auth(key))
    assert r.status_code == 200
    assert _tenant_obj(tid).claim_grace_hours == 6
    # the pending claim (no override) was re-placed under the new rule → queued
    assert _claims(tid)[0].stage == "queued"
    # bad mode rejected
    assert client.post("/v1/array-owners/claims/settings",
                       json={"sendMode": "nope"}, headers=_auth(key)).status_code == 400


def test_send_resolve_dismiss_reopen_flow(client):
    tid, key = _make_tenant()
    _seed(tid, [_inv(901, "dead")])
    cid = _claims(tid)[0].id

    # approve & send
    r = client.post(f"/v1/array-owners/claims/{cid}/send", headers=_auth(key))
    assert r.status_code == 200 and r.json()["claim"]["stage"] == "sent"

    # resolve with an explicit recovered value
    r = client.post(f"/v1/array-owners/claims/{cid}/resolve",
                    json={"recoveredUsd": 1234}, headers=_auth(key))
    assert r.json()["claim"]["stage"] == "resolved"
    assert r.json()["claim"]["recoveredUsd"] == 1234

    # reopen → back to ready (manual policy)
    r = client.post(f"/v1/array-owners/claims/{cid}/reopen", headers=_auth(key))
    assert r.json()["claim"]["stage"] == "ready"

    # dismiss
    r = client.post(f"/v1/array-owners/claims/{cid}/dismiss", headers=_auth(key))
    assert r.json()["claim"]["stage"] == "dismissed"


def test_per_claim_mode_override_and_cancel_auto(client):
    tid, key = _make_tenant()                               # global manual
    _seed(tid, [_inv(1001, "dead")])
    cid = _claims(tid)[0].id
    # override THIS claim to delay → queued even though global is manual
    r = client.post(f"/v1/array-owners/claims/{cid}/mode",
                    json={"mode": "delay"}, headers=_auth(key))
    assert r.json()["claim"]["stage"] == "queued"
    assert r.json()["claim"]["mode"] == "delay"
    # cancel the auto-send → held back to ready, override cleared to manual
    r = client.post(f"/v1/array-owners/claims/{cid}/cancel-auto", headers=_auth(key))
    assert r.json()["claim"]["stage"] == "ready"


def test_draft_edits_persist(client):
    tid, key = _make_tenant()
    _seed(tid, [_inv(1101, "dead")])
    cid = _claims(tid)[0].id
    r = client.patch(f"/v1/array-owners/claims/{cid}/draft",
                     json={"to": "warranty@installer.example", "subject": "Custom"},
                     headers=_auth(key))
    assert r.status_code == 200
    d = r.json()["claim"]["draft"]
    assert d["to"] == "warranty@installer.example" and d["subject"] == "Custom"
    # body untouched
    assert "warranty claim" in d["body"].lower()


def test_cross_tenant_access_is_404(client):
    tid_a, _ = _make_tenant()
    _seed(tid_a, [_inv(1201, "dead")])
    cid = _claims(tid_a)[0].id
    _, key_b = _make_tenant()
    assert client.post(f"/v1/array-owners/claims/{cid}/send",
                       headers=_auth(key_b)).status_code == 404
