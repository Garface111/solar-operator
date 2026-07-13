"""Central Maine Power (CMP) Cloud Capture — registry, routing, sniffer, allowlist."""
from __future__ import annotations

import secrets
from types import SimpleNamespace

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.harvester.login import HINTS, hint_key_for
from api.harvester.vendors import module_for
from api.harvester.vendors.cmp import CMPVendor
from api.models import Tenant
from api.providers import BESPOKE_LIVE_CODES, PROVIDERS


def test_cmp_is_bespoke_live_in_catalog():
    assert "cmp" in BESPOKE_LIVE_CODES
    rows = [p for p in PROVIDERS if p["code"] == "cmp"]
    assert rows
    assert rows[0]["scrape_status"] == "live"
    assert not rows[0]["smarthub_host"]
    assert "cmpco.com" in (rows[0].get("portal_url") or "")


def test_module_for_routes_cmp():
    m = module_for("cmp")
    assert m is not None
    assert m.provider == "cmp"
    assert m.__class__.__name__ == "CMPVendor"
    # Not SmartHub fallthrough
    assert module_for("cmp") is not module_for("vec")


def test_login_hints_for_cmp():
    assert hint_key_for("cmp") == "cmp"
    assert "cmp" in HINTS
    assert "password" in HINTS["cmp"]["pass"].lower()


def test_sniffer_extracts_accounts_and_daily_generation():
    v = CMPVendor()
    payload = {
        "accounts": [{
            "accountNumber": "4411223344",
            "nickname": "Farmstead",
            "totalGrossGenerated": 880.0,
            "intervals": [{
                "values": [
                    {"date": "2026-06-10", "returnedGeneration": 8.1},
                    {"date": "2026-06-11", "receivedKwh": 7.4},
                    {"date": "2026-06-12", "returnedGeneration": 0},
                ]
            }],
        }]
    }
    accts = v._extract_accounts([("https://portal.cmpco.com/api/usage", payload)])
    assert len(accts) == 1
    a = accts[0]
    assert a["account_number"] == "4411223344"
    assert a["nickname"] == "Farmstead"
    days = {d["date"]: d["generated_kwh"] for d in a["daily"]}
    assert days["2026-06-10"] == 8.1
    assert days["2026-06-11"] == 7.4
    assert "2026-06-12" not in days


def test_utility_meter_capture_accepts_cmp(client):
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="CMP Owner",
            contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    auth = {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}
    body = {
        "provider": "cmp",
        "accounts": [{
            "account_number": "9988776655",
            "nickname": "Barn",
            "summary": {"totalGrossGenerated": 50, "isNetMetered": True},
            "daily": [
                {"date": "2026-05-01", "generated_kwh": 5.0},
                {"date": "2026-05-02", "generated_kwh": 6.5},
            ],
        }],
    }
    r = client.post(
        "/v1/array-owners/utility-meter-capture",
        headers=auth, json=body,
    )
    assert r.status_code == 200, r.text


def test_cloud_capture_save_cmp_no_login_host(client, monkeypatch):
    monkeypatch.setenv("CLOUD_CAPTURE_COLLECT", "1")
    monkeypatch.setenv("CLOUD_CAPTURE_ENABLED", "1")
    monkeypatch.setattr("api.cloud_capture.cc.crypto_ready", lambda: True)
    monkeypatch.setattr(
        "api.cloud_capture.cc.upsert_credential",
        lambda db, tid, provider, username, password, login_host=None, enable=True: (
            SimpleNamespace(provider=provider, username=username)
        ),
    )
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="CMP Cloud",
            contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    auth = {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}
    r = client.post(
        "/v1/cloud-capture/credentials",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "provider": "cmp",
            "username": "owner@example.com",
            "password": "secret-pass",
            "consent": True,
            "enable": True,
        },
    )
    assert r.status_code != 422, r.text
    assert r.status_code in (200, 403, 409), r.text
    if r.status_code == 200:
        assert r.json().get("provider") == "cmp"
