"""Tests for the signoff-from-master fix.

Verifies that:
  - build_context() uses tenant_signoff_name inside the signoff sub-render
    but leaves {{tenant_name}} in the body unchanged.
  - POST /v1/account/send-from-name updates Tenant.send_from_name correctly.
"""
from __future__ import annotations

import secrets

import pytest

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.email_templates import DEFAULT_SIGNOFF, build_context
from api.models import Tenant


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tenant(*, send_from_name: str | None = None) -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Green Mountain Community Solar",
            contact_email=f"{tid}@test.example",
            tenant_key=key,
            plan="standard",
            active=True,
            send_from_name=send_from_name,
        ))
        db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _get_send_from_name(tid: str) -> str | None:
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        return t.send_from_name


# ── build_context() unit tests ────────────────────────────────────────────────


def test_signoff_uses_send_from_name_when_set():
    ctx = build_context(
        client_name="Acme Solar",
        tenant_name="GMCS",
        arrays_count=2,
        tenant_signoff_name="Bruce G.",
    )
    assert "Bruce G." in ctx["signoff"]
    assert "GMCS" not in ctx["signoff"]


def test_signoff_falls_back_to_tenant_name_when_send_from_name_null():
    ctx = build_context(
        client_name="Acme Solar",
        tenant_name="GMCS",
        arrays_count=2,
        tenant_signoff_name=None,
    )
    assert "GMCS" in ctx["signoff"]


def test_signoff_falls_back_when_send_from_name_empty_string():
    ctx = build_context(
        client_name="Acme Solar",
        tenant_name="GMCS",
        arrays_count=2,
        tenant_signoff_name="   ",
    )
    assert "GMCS" in ctx["signoff"]


def test_body_tenant_name_is_unchanged_by_send_from_name():
    """{{tenant_name}} in the body must still resolve to the company name."""
    body_template = "<p>Report from {{tenant_name}}'s desk.</p>{{signoff}}"
    ctx = build_context(
        client_name="Acme Solar",
        tenant_name="GMCS",
        arrays_count=2,
        tenant_signoff_name="Bruce G.",
    )
    from api.email_templates import render_merge
    rendered = render_merge(body_template, ctx)
    assert "GMCS" in rendered          # body uses company name
    assert "Bruce G." in rendered      # signoff uses personal name


# ── POST /v1/account/send-from-name endpoint tests ───────────────────────────


class TestUpdateSendFromName:
    def test_success(self, client):
        tid, auth = _make_tenant()
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": "Bruce Genereaux"},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["send_from_name"] == "Bruce Genereaux"
        assert _get_send_from_name(tid) == "Bruce Genereaux"

    def test_strips_whitespace(self, client):
        tid, auth = _make_tenant()
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": "  Bruce G.  "},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["send_from_name"] == "Bruce G."
        assert _get_send_from_name(tid) == "Bruce G."

    def test_rejects_too_long(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": "x" * 121},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"].lower()

    def test_exactly_120_chars_allowed(self, client):
        _, auth = _make_tenant()
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": "a" * 120},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert len(resp.json()["send_from_name"]) == 120

    def test_empty_string_clears_to_null(self, client):
        tid, auth = _make_tenant(send_from_name="Old Name")
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": ""},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["send_from_name"] is None
        assert _get_send_from_name(tid) is None

    def test_null_clears_to_null(self, client):
        tid, auth = _make_tenant(send_from_name="Old Name")
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": None},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["send_from_name"] is None
        assert _get_send_from_name(tid) is None

    def test_whitespace_only_clears_to_null(self, client):
        tid, auth = _make_tenant(send_from_name="Old Name")
        resp = client.post(
            "/v1/account/send-from-name",
            json={"send_from_name": "   "},
            headers={"Authorization": auth},
        )
        assert resp.status_code == 200
        assert resp.json()["send_from_name"] is None
        assert _get_send_from_name(tid) is None
