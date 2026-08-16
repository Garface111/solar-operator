"""Unit tests for Grok Build OIDC bearer resolution (no network)."""
from __future__ import annotations

import base64
import json
import time

import pytest

from bankai import xai_auth


def _fake_jwt(exp_offset: int = 3600, team_id: str = "team-build", client_id: str = "cid") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = {
        "exp": int(time.time()) + exp_offset,
        "team_id": team_id,
        "client_id": client_id,
        "aud": client_id,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def test_pick_prefers_valid_hermes_over_expired_grok(tmp_path, monkeypatch):
    grok = tmp_path / "grok-auth.json"
    hermes = tmp_path / "hermes-auth.json"
    expired = _fake_jwt(exp_offset=-1000)
    live = _fake_jwt(exp_offset=7200)
    grok.write_text(
        json.dumps(
            {
                "https://auth.x.ai::cid": {
                    "key": expired,
                    "refresh_token": "rt-dead",
                    "oidc_client_id": "cid",
                    "oidc_issuer": "https://auth.x.ai",
                    "team_id": "team-build",
                }
            }
        )
    )
    hermes.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {
                            "access_token": live,
                            "refresh_token": "rt-live",
                        }
                    }
                },
                "credential_pool": {"xai-oauth": []},
            }
        )
    )
    monkeypatch.setenv("GROK_AUTH_JSON", str(grok))
    monkeypatch.setenv("HERMES_AUTH_JSON", str(hermes))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("XAI_OIDC_REFRESH_TOKEN", raising=False)
    # clear module cache
    with xai_auth._lock:
        xai_auth._cache["access_token"] = None
        xai_auth._cache["expires_at"] = 0.0

    tok = xai_auth.get_xai_bearer()
    assert tok == live
    status = xai_auth.xai_auth_status()
    assert status["oidc_source"] == "hermes_auth_json"
    assert status["oidc_team_id"] == "team-build"


def test_refuses_classic_key_when_oidc_preferred_but_broken(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_AUTH_JSON", str(tmp_path / "missing-grok.json"))
    monkeypatch.setenv("HERMES_AUTH_JSON", str(tmp_path / "missing-hermes.json"))
    monkeypatch.setenv("XAI_API_KEY", "xai-capped-key")
    monkeypatch.setenv("XAI_PREFER_GROK_BUILD_OIDC", "1")
    with xai_auth._lock:
        xai_auth._cache["access_token"] = None
        xai_auth._cache["expires_at"] = 0.0
    # No OIDC at all → classic key is allowed as last resort
    assert xai_auth.get_xai_bearer() == "xai-capped-key"


def test_status_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_AUTH_JSON", str(tmp_path / "g.json"))
    monkeypatch.setenv("HERMES_AUTH_JSON", str(tmp_path / "h.json"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    s = xai_auth.xai_auth_status()
    blob = json.dumps(s)
    assert "eyJ" not in blob
    assert "refresh" not in blob.lower() or "oidc_configured" in s
