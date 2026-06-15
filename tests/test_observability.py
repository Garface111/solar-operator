"""Tests for error monitoring / global exception handling (launch readiness).

Covers:
  * The global exception handler returns a clean JSON 500 (no stack trace leak)
    and routes the error to capture_exception + the throttled email alert.
  * HTTPExceptions (401/404/etc.) are NOT swallowed by the catch-all.
  * Sentry is a silent no-op without SENTRY_DSN (init returns False, is_enabled
    False) — so dev/tests/prod-without-DSN are completely unaffected.
  * The PII scrubber redacts sensitive header/body keys before send.
  * /health advertises sentry_configured.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import observability


# ─── Sentry init is optional / safe ─────────────────────────────────────────────

def test_init_sentry_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert observability.init_sentry() is False
    assert observability.is_enabled() is False


def test_capture_exception_noop_when_disabled(monkeypatch):
    # Force-disabled state; must not raise even though no SDK is initialized.
    monkeypatch.setattr(observability, "_ENABLED", False)
    observability.capture_exception(ValueError("boom"))  # no exception = pass


# ─── PII scrubbing ──────────────────────────────────────────────────────────────

def test_scrub_redacts_sensitive_keys():
    raw = {
        "Authorization": "Bearer secrettoken",
        "password": "hunter2hunter2",
        "nested": {"access_token": "abc", "safe": "keep-me"},
        "list": [{"cookie": "x"}, {"ok": 1}],
    }
    out = observability._scrub(raw)
    assert out["Authorization"] == "[redacted]"
    assert out["password"] == "[redacted]"
    assert out["nested"]["access_token"] == "[redacted]"
    assert out["nested"]["safe"] == "keep-me"
    assert out["list"][0]["cookie"] == "[redacted]"
    assert out["list"][1]["ok"] == 1


def test_before_send_scrubs_request_headers():
    event = {"request": {"headers": {"authorization": "Bearer x", "host": "api"}}}
    out = observability._before_send(event, {})
    assert out["request"]["headers"]["authorization"] == "[redacted]"
    assert out["request"]["headers"]["host"] == "api"


# ─── Global exception handler ───────────────────────────────────────────────────

@pytest.fixture()
def app_with_probe_routes():
    """The real app, plus two throwaway routes that raise — used to exercise the
    global handler. raise_server_exceptions=False so the handler (not the test
    client) produces the response, mirroring prod."""
    from api.app import app

    @app.get("/v1/_test_boom")
    def _boom():
        raise RuntimeError("intentional test explosion")

    @app.get("/v1/_test_http_404")
    def _not_found():
        raise HTTPException(404, "nope")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_error_returns_clean_json_500(app_with_probe_routes, monkeypatch):
    sent = {}

    def _fake_alert(subject, body):
        sent["subject"] = subject
        return True

    captured = {}
    monkeypatch.setattr(observability, "capture_exception", lambda e: captured.setdefault("exc", e))
    # patch the symbol imported into app.py too
    import api.app as appmod
    monkeypatch.setattr(appmod, "capture_exception", lambda e: captured.setdefault("exc", e))
    monkeypatch.setattr("api.notify.send_internal_alert", _fake_alert)
    # reset throttle so the alert fires
    appmod._LAST_ALERT.clear()

    r = app_with_probe_routes.get("/v1/_test_boom")
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert "went wrong" in body["error"].lower()
    # no stack trace / exception text leaked to the client
    assert "intentional test explosion" not in r.text
    assert "RuntimeError" not in r.text
    # error was captured + alerted
    assert "exc" in captured
    assert "RuntimeError" in sent.get("subject", "")


def test_http_exception_not_swallowed(app_with_probe_routes):
    r = app_with_probe_routes.get("/v1/_test_http_404")
    assert r.status_code == 404  # normal control flow, not turned into a 500


def test_alert_is_throttled(app_with_probe_routes, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda s, b: calls.__setitem__("n", calls["n"] + 1) or True)
    import api.app as appmod
    monkeypatch.setattr(appmod, "capture_exception", lambda e: None)
    appmod._LAST_ALERT.clear()

    for _ in range(3):
        app_with_probe_routes.get("/v1/_test_boom")
    # Same path+type within the cooldown window → only ONE email despite 3 errors.
    assert calls["n"] == 1


# ─── /health advertises the flag ────────────────────────────────────────────────

def test_health_reports_sentry_configured(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "sentry_configured" in r.json()


# ─── /v1/client-error (browser/extension → backend pipeline) ────────────────────

def test_client_error_accepts_and_alerts(client, monkeypatch):
    sent = {}
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda s, b: sent.update(subject=s, body=b) or True)
    import api.app as appmod
    appmod._LAST_ALERT.clear()
    r = client.post("/v1/client-error", json={
        "source": "arrayoperator", "message": "TypeError: x is undefined",
        "stack": "at foo (sandbox.js:10)", "url": "https://arrayoperator.com/",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "arrayoperator" in sent.get("subject", "")
    assert "TypeError" in sent.get("subject", "")


def test_client_error_ignores_empty(client):
    r = client.post("/v1/client-error", json={"source": "extension"})
    assert r.status_code == 200
    assert r.json().get("ignored") == "empty"


def test_client_error_caps_payload(client, monkeypatch):
    body_seen = {}
    monkeypatch.setattr("api.notify.send_internal_alert",
                        lambda s, b: body_seen.update(b=b) or True)
    import api.app as appmod
    appmod._LAST_ALERT.clear()
    huge = "A" * 9000
    r = client.post("/v1/client-error", json={"source": "x", "message": huge, "stack": huge})
    assert r.status_code == 200
    # message capped at 500, stack at 4000 → never the full 9000 (small slack for
    # any boilerplate; the point is the 9000-char flood is clipped to ~4500).
    assert body_seen["b"].count("A") <= 500 + 4000 + 50

