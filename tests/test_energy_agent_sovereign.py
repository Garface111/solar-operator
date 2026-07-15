"""Sovereign Mind — dark by default; gates refuse act/speak without flags."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_sovereign_env(monkeypatch):
    for k in (
        "SOVEREIGN_ENABLED",
        "SOVEREIGN_ACT_ENABLED",
        "SOVEREIGN_SPEAK_ENABLED",
        "SOVEREIGN_SENSE_ENABLED",
        "SOVEREIGN_CAPABILITIES",
        "SOVEREIGN_ARM_T4_T5",
        "SOVEREIGN_SERVICE_KEY",
        "ADMIN_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_sovereign_disabled_by_default():
    from api.energy_agent_sovereign import (
        capability_allowed,
        plan_action,
        plan_inject,
        sovereign_enabled,
        sovereign_tick,
    )

    assert sovereign_enabled() is False
    assert capability_allowed("sense.queues") is False
    assert capability_allowed("speak.session_inject") is False
    assert capability_allowed("act.money_identity") is False

    out = sovereign_tick(reason="test")
    assert out["mode"] == "dark"
    assert out["enabled"] is False
    assert out["decisions"] == []

    inj = plan_inject(tenant_ids=["ten_x"], speak="hello")
    assert inj["denied"] is True

    act = plan_action("act.soft_stage", {})
    assert act["denied"] is True


def test_money_never_autonomous_even_when_armed(monkeypatch):
    from api.energy_agent_sovereign import plan_action

    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ARM_T4_T5", "1")
    monkeypatch.setenv("SOVEREIGN_CAPABILITIES", "*")

    out = plan_action("act.money_identity", {"do": "bad"})
    assert out["denied"] is True
    assert "never" in out["denied_reason"].lower() or "dual" in out["denied_reason"].lower()


def test_admin_state_requires_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-sovereign")
    from api.app import app

    client = TestClient(app)
    r = client.get("/admin/sovereign/state")
    assert r.status_code in (401, 403)

    r2 = client.get(
        "/admin/sovereign/state",
        headers={"Authorization": "Bearer test-admin-key-sovereign"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["enabled"] is False
    assert body["mode"] == "dark"
    assert "architecture" in body
    assert "sense.queues" in body["capabilities"]


def test_tick_ep_dark(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-sovereign")
    from api.app import app

    client = TestClient(app)
    r = client.post(
        "/admin/sovereign/tick",
        headers={"Authorization": "Bearer test-admin-key-sovereign"},
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "dark"


def test_capability_allowlist_sense_only(monkeypatch):
    from api.energy_agent_sovereign import capability_allowed

    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SENSE_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_SPEAK_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_ACT_ENABLED", "1")
    # no CAPABILITIES list → sense only when SENSE_ENABLED
    assert capability_allowed("sense.queues") is True
    assert capability_allowed("speak.session_inject") is False
    assert capability_allowed("act.soft_stage") is False

    monkeypatch.setenv("SOVEREIGN_CAPABILITIES", "speak.session_inject")
    assert capability_allowed("speak.session_inject") is True
    assert capability_allowed("sense.queues") is False
