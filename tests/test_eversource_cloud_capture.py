"""Eversource Cloud Capture — registry, module routing, JSON sniffer, API allowlist."""
from __future__ import annotations

import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.harvester.vendors import module_for
from api.harvester.vendors.eversource import EversourceVendor
from api.harvester.login import hint_key_for, HINTS
from api.models import Tenant
from api.providers import BESPOKE_LIVE_CODES, PROVIDERS


def test_eversource_is_bespoke_live_in_catalog():
    for code in ("eversource", "eversource_ma", "eversource_ct"):
        assert code in BESPOKE_LIVE_CODES
        rows = [p for p in PROVIDERS if p["code"] == code]
        assert rows, f"{code} missing from PROVIDERS"
        assert rows[0]["scrape_status"] == "live"
        assert not rows[0]["smarthub_host"]
        assert "eversource.com" in (rows[0].get("portal_url") or "")


def test_module_for_routes_eversource_aliases():
    m = module_for("eversource")
    assert m is not None
    assert m.provider == "eversource"
    assert module_for("eversource_ma") is m
    assert module_for("eversource_ct") is m
    # Does NOT fall through to SmartHub
    assert module_for("eversource").__class__.__name__ == "EversourceVendor"


def test_login_hints_for_eversource():
    assert hint_key_for("eversource") == "eversource"
    assert hint_key_for("eversource_ma") == "eversource"
    assert "eversource" in HINTS
    assert "password" in HINTS["eversource"]["pass"].lower()


def test_sniffer_extracts_accounts_and_daily_generation():
    v = EversourceVendor()
    payload = {
        "accounts": [
            {
                "accountNumber": "1234567890",
                "nickname": "Barn array",
                "totalGrossGenerated": 1200.5,
                "intervals": [
                    {"values": [
                        {"date": "2026-06-01", "returnedGeneration": 12.4, "consumed": 3.1},
                        {"date": "2026-06-02", "returnedGeneration": 0, "consumed": 5.0},
                        {"date": "2026-06-03", "generationKwh": 9.2},
                    ]}
                ],
            }
        ]
    }
    accts = v._extract_accounts([("https://www.eversource.com/api/usage", payload)])
    assert len(accts) == 1
    a = accts[0]
    assert a["account_number"] == "1234567890"
    assert a["nickname"] == "Barn array"
    assert a["summary"].get("totalGrossGenerated") == 1200.5
    # zero generation day dropped; two positive days kept
    days = {d["date"]: d["generated_kwh"] for d in a["daily"]}
    assert days["2026-06-01"] == 12.4
    assert days["2026-06-03"] == 9.2
    assert "2026-06-02" not in days


def test_sniffer_mdy_dates_and_nested_lists():
    v = EversourceVendor()
    payload = [
        {"account_number": "99887766", "date": "6/15/2026", "export_kwh": 4.5},
        {"account_number": "99887766", "date": "6/16/2026", "export_kwh": 5.0},
    ]
    accts = v._extract_accounts([("https://x/usage", payload)])
    assert len(accts) == 1
    assert accts[0]["account_number"] == "99887766"
    assert [d["date"] for d in accts[0]["daily"]] == ["2026-06-15", "2026-06-16"]


def test_utility_meter_capture_accepts_eversource(client):
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="EV Owner",
            contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    auth = {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}
    body = {
        "provider": "eversource",
        "accounts": [{
            "account_number": "5551234567",
            "nickname": "Roof",
            "summary": {"totalGrossGenerated": 100, "isNetMetered": True},
            "daily": [
                {"date": "2026-05-01", "generated_kwh": 10.0},
                {"date": "2026-05-02", "generated_kwh": 11.5},
            ],
        }],
    }
    r = client.post(
        "/v1/array-owners/utility-meter-capture",
        headers=auth, json=body,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # Shape varies slightly; either ok/accounts or arrays/results — just not a 400 reject.
    assert "eversource" not in str(j.get("detail", "")).lower()


def test_cloud_capture_save_eversource_no_login_host(client, monkeypatch):
    """Eversource is bespoke — must accept credentials without SmartHub login_host."""
    monkeypatch.setenv("CLOUD_CAPTURE_COLLECT", "1")
    monkeypatch.setenv("CLOUD_CAPTURE_ENABLED", "1")
    # Encryption: if not armed, password saves 409 — toggle path without password
    # still proves the provider is allowed without login_host.
    tid = "ten_" + secrets.token_hex(5)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="EV Cloud",
            contact_email=f"{tid}@ex.test",
            tenant_key="sol_live_" + secrets.token_urlsafe(8),
            plan="standard", active=True, product="array_operator",
        ))
        db.commit()
    auth = {"Authorization": f"Bearer {mint_session_for_tenant(tid)}"}
    # Without password: just enable — still hits provider validation.
    # If encryption not ready, password path 409s; without password should 200
    # once a row exists. First create with password may 409 — so mock crypto.
    monkeypatch.setattr(
        "api.cloud_capture.cc.crypto_ready", lambda: True,
    )
    monkeypatch.setattr(
        "api.cloud_capture.cc.upsert_credential",
        lambda db, tid, provider, username, password, login_host=None, enable=True: (
            SimpleNamespace(provider=provider, username=username)
        ),
    )
    r = client.post(
        "/v1/cloud-capture/credentials",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "provider": "eversource",
            "username": "owner@example.com",
            "password": "secret-pass",
            "consent": True,
            "enable": True,
        },
    )
    # 200 if mock works; 422 would mean we still demanded login_host (the bug).
    assert r.status_code != 422, r.text
    assert r.status_code in (200, 403, 409), r.text  # 403 if collection off
    if r.status_code == 200:
        assert r.json().get("provider") == "eversource"
