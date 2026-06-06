"""
Tests for the email template studio endpoints:
  GET  /v1/account/reports/email-template
  PUT  /v1/account/reports/email-template
  POST /v1/account/reports/email-template/preview
  POST /v1/account/reports/email-template/chat
  POST /v1/account/reports/email-template/test-send
  POST /v1/account/reports/email-template/reset
  PUT  /v1/account/reports/email-template/signoff
"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select

from api.account import mint_session_for_tenant
from api.db import SessionLocal
from api.email_templates import (
    DEFAULT_BODY_TEMPLATE,
    DEFAULT_SIGNOFF,
    DEFAULT_SUBJECT_TEMPLATE,
    render_merge,
)
from api.models import Client, Tenant


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tenant(*, with_client: bool = False) -> tuple[str, str]:
    """Create a fresh active tenant; return (tenant_id, Bearer header)."""
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="Template Test Co",
            contact_email=f"{tid}@test.example",
            tenant_key=key,
            plan="standard",
            active=True,
        ))
        db.commit()
    if with_client:
        with SessionLocal() as db:
            db.add(Client(
                tenant_id=tid,
                name="Evergreen Solar",
                contact_email=f"client_{tid}@evergreen.example",
                active=True,
            ))
            db.commit()
    return tid, f"Bearer {mint_session_for_tenant(tid)}"


def _auth(tid: str) -> str:
    return f"Bearer {mint_session_for_tenant(tid)}"


# ── (1) GET resolves defaults when all three fields are null ──────────────────


def test_get_returns_defaults_when_null(client):
    _, auth = _make_tenant()
    resp = client.get("/v1/account/reports/email-template",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject_template"] == DEFAULT_SUBJECT_TEMPLATE
    assert body["body_template"] == DEFAULT_BODY_TEMPLATE
    assert body["signoff"] == DEFAULT_SIGNOFF
    assert body["is_default_subject"] is True
    assert body["is_default_body"] is True
    assert body["is_default_signoff"] is True
    assert body["is_default"] is True


# ── (2) PUT round-trip — set custom subject + body, GET returns them ──────────


def test_put_round_trip(client):
    _, auth = _make_tenant()
    custom_subject = "Your {{quarter}} report from {{tenant_name}}"
    custom_body = "<p>Hello {{client_name}}, here is your report.</p>{{signoff}}"

    resp = client.put(
        "/v1/account/reports/email-template",
        json={"subject_template": custom_subject, "body_template": custom_body},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["subject_template"] == custom_subject
    assert data["body_template"] == custom_body

    get_resp = client.get("/v1/account/reports/email-template",
                          headers={"Authorization": auth})
    assert get_resp.status_code == 200, get_resp.text
    get_data = get_resp.json()
    assert get_data["subject_template"] == custom_subject
    assert get_data["body_template"] == custom_body
    assert get_data["is_default_subject"] is False
    assert get_data["is_default_body"] is False
    assert get_data["is_default"] is False


# ── (3) preview renders defaults and render_merge is callable ─────────────────


def test_preview_renders_defaults(client):
    tid, auth = _make_tenant(with_client=True)
    resp = client.post(
        "/v1/account/reports/email-template/preview",
        json={},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "subject_rendered" in body
    assert "body_rendered" in body
    # Defaults must have rendered something non-empty
    assert body["subject_rendered"]
    assert body["body_rendered"]


def test_render_merge_is_importable():
    """Smoke-test: the import that caused today's 500 is reachable."""
    result = render_merge("Hello {{name}}", {"name": "World"})
    assert result == "Hello World"


def test_preview_with_custom_subject(client):
    tid, auth = _make_tenant(with_client=True)
    resp = client.post(
        "/v1/account/reports/email-template/preview",
        json={"subject_template": "Hi {{client_name}}, your report"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rendered = body["subject_rendered"]
    assert rendered
    # merge token must be substituted — must not appear literally
    assert "{{client_name}}" not in rendered


# ── (4) merge-token preservation: {{client_name}} is replaced with real name ──


def test_preview_merge_token_replaced(client):
    tid, auth = _make_tenant()
    # Add a client with a known name
    with SessionLocal() as db:
        db.add(Client(
            tenant_id=tid,
            name="Meadow Farm Co-op",
            contact_email="meadow@farm.example",
            active=True,
        ))
        db.commit()

    resp = client.post(
        "/v1/account/reports/email-template/preview",
        json={"subject_template": "Hi {{client_name}}, your report"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    rendered = resp.json()["subject_rendered"]
    assert "Meadow Farm Co-op" in rendered
    assert "{{client_name}}" not in rendered


# ── (5) signoff PUT — set signoff, GET shows it, preview body includes it ─────


def test_signoff_round_trip_and_preview(client):
    tid, auth = _make_tenant(with_client=True)
    custom_signoff = "<p>Best,<br>{{tenant_name}}</p>"

    put_resp = client.put(
        "/v1/account/reports/email-template/signoff",
        json={"signoff": custom_signoff},
        headers={"Authorization": auth},
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["ok"] is True
    assert put_resp.json()["signoff"] == custom_signoff

    get_resp = client.get("/v1/account/reports/email-template",
                          headers={"Authorization": auth})
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["signoff"] == custom_signoff
    assert get_resp.json()["is_default_signoff"] is False

    preview_resp = client.post(
        "/v1/account/reports/email-template/preview",
        json={},
        headers={"Authorization": auth},
    )
    assert preview_resp.status_code == 200, preview_resp.text
    body_rendered = preview_resp.json()["body_rendered"]
    # signoff block should be rendered into body
    assert "Best," in body_rendered


# ── (6) reset clears all three to None ───────────────────────────────────────


def test_reset_clears_all(client):
    _, auth = _make_tenant()
    # First set custom values
    client.put(
        "/v1/account/reports/email-template",
        json={"subject_template": "custom subject", "body_template": "<p>custom</p>"},
        headers={"Authorization": auth},
    )
    client.put(
        "/v1/account/reports/email-template/signoff",
        json={"signoff": "<p>Custom signoff</p>"},
        headers={"Authorization": auth},
    )

    # Now reset
    reset_resp = client.post(
        "/v1/account/reports/email-template/reset",
        headers={"Authorization": auth},
    )
    assert reset_resp.status_code == 200, reset_resp.text
    assert reset_resp.json()["ok"] is True

    # GET should now show defaults
    get_resp = client.get("/v1/account/reports/email-template",
                          headers={"Authorization": auth})
    assert get_resp.status_code == 200, get_resp.text
    data = get_resp.json()
    assert data["subject_template"] == DEFAULT_SUBJECT_TEMPLATE
    assert data["body_template"] == DEFAULT_BODY_TEMPLATE
    assert data["signoff"] == DEFAULT_SIGNOFF
    assert data["is_default"] is True


# ── (7) chat endpoint — LLM is mocked, returns expected shape ────────────────


def test_chat_calls_ai_and_returns_result(client, monkeypatch):
    import api.account as account_mod

    fake_result = {
        "reply": "I made it more concise.",
        "body": "<p>Updated body</p>",
        "subject": None,
    }

    def fake_regen(**kwargs):
        return fake_result

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(
        "api.email_templates.regenerate_template_via_ai", fake_regen
    )
    # Also patch within account module's local import path
    import api.email_templates as et_mod
    monkeypatch.setattr(et_mod, "regenerate_template_via_ai", fake_regen)

    _, auth = _make_tenant()
    resp = client.post(
        "/v1/account/reports/email-template/chat",
        json={
            "messages": [{"role": "user", "content": "Make it shorter"}],
            "current_body": "<p>Original</p>",
        },
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "assistant_reply" in data
    assert "proposed_body" in data
    assert "proposed_subject" in data


def test_chat_returns_503_without_api_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _, auth = _make_tenant()
    resp = client.post(
        "/v1/account/reports/email-template/chat",
        json={
            "messages": [{"role": "user", "content": "Make it shorter"}],
            "current_body": "<p>Original</p>",
        },
        headers={"Authorization": auth},
    )
    assert resp.status_code == 503


# ── (8) test-send — Resend is mocked ─────────────────────────────────────────


def test_test_send_dispatches_email(client, monkeypatch):
    import api.notify as notify_mod

    sent_calls = []

    def fake_send(**kw):
        sent_calls.append(kw)
        return True

    # The test-send handler does a local `from .notify import _send_via_resend`
    # inside the function body, so we must patch it at the source module.
    monkeypatch.setattr(notify_mod, "_send_via_resend", fake_send)

    tid, auth = _make_tenant(with_client=True)
    resp = client.post(
        "/v1/account/reports/email-template/test-send",
        json={},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert len(sent_calls) == 1
    assert "[TEST]" in sent_calls[0]["subject"]


def test_default_body_has_no_dashboard_signoff(client):
    _, auth = _make_tenant()
    resp = client.get("/v1/account/reports/email-template",
                      headers={"Authorization": auth})
    assert resp.status_code == 200, resp.text
    body_template = resp.json()["body_template"]
    assert "your dashboard" not in body_template.lower()
    assert "dashboard_url" not in body_template


def test_put_rejects_dashboard_url_tag(client):
    _, auth = _make_tenant()
    resp = client.put(
        "/v1/account/reports/email-template",
        json={"body_template": "<p>Hi {{client_name}}, manage at {{dashboard_url}}.</p>"},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 422, resp.text


def test_test_send_422_without_tenant_email(client, monkeypatch):
    import api.account as account_mod
    monkeypatch.setattr(account_mod, "_send_via_resend", lambda **kw: True)

    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid,
            name="No Email Co",
            contact_email="",
            tenant_key=key,
            plan="standard",
            active=True,
        ))
        db.commit()
    auth = f"Bearer {mint_session_for_tenant(tid)}"

    resp = client.post(
        "/v1/account/reports/email-template/test-send",
        json={},
        headers={"Authorization": auth},
    )
    assert resp.status_code == 422
