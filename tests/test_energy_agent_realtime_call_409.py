"""realtime-call: OpenAI 409 call_id_in_use is a client conflict, not a 502."""
from __future__ import annotations

import asyncio
import io
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import energy_agent as ea


def _sdp_request(body: bytes = b"v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n") -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/energy-agent/realtime-call",
        "raw_path": b"/v1/energy-agent/realtime-call",
        "query_string": b"",
        "headers": [(b"content-type", b"application/sdp")],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _patch_auth_budget(monkeypatch):
    monkeypatch.setattr(ea, "OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setattr(
        ea, "_auth", lambda authorization: SimpleNamespace(id="ten_test")
    )

    class _FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ea, "SessionLocal", _FakeDB)
    monkeypatch.setattr(ea, "_check_budget", lambda db, tid: {"ok": True})


def test_realtime_call_maps_openai_409_to_client_conflict(monkeypatch):
    """Same SDP re-posted while live → OpenAI 409 call_id_in_use → our 409, not 502."""
    _patch_auth_budget(monkeypatch)

    upstream = (
        b'{\n    "error": {\n        "message": "A live session already exists for the '
        b'provided call_id.",\n        "type": "invalid_request_error",\n'
        b'        "code": "call_id_in_use",\n        "param": ""\n    }\n}'
    )

    def boom(req, timeout=45):
        raise urllib.error.HTTPError(
            url="https://api.openai.com/v1/realtime/calls",
            code=409,
            msg="Conflict",
            hdrs=None,
            fp=io.BytesIO(upstream),
        )

    monkeypatch.setattr(ea.urllib.request, "urlopen", boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ea.realtime_call(_sdp_request(), authorization="Bearer t"))

    assert ei.value.status_code == 409
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["error"] == "realtime_session_in_use"
    assert "fresh" in detail["message"].lower() or "hang up" in detail["message"].lower()


def test_realtime_call_other_openai_errors_still_502(monkeypatch):
    """Non-409 OpenAI failures stay 502 (upstream outage / bad config)."""
    _patch_auth_budget(monkeypatch)

    def boom(req, timeout=45):
        raise urllib.error.HTTPError(
            url="https://api.openai.com/v1/realtime/calls",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"boom"}}'),
        )

    monkeypatch.setattr(ea.urllib.request, "urlopen", boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ea.realtime_call(_sdp_request(), authorization="Bearer t"))

    assert ei.value.status_code == 502
    assert "500" in str(ei.value.detail)


def test_realtime_call_success_returns_sdp(monkeypatch):
    _patch_auth_budget(monkeypatch)

    answer = "v=0\r\no=- 9 2 IN IP4 0.0.0.0\r\ns=-\r\n"

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return answer.encode()

    monkeypatch.setattr(ea.urllib.request, "urlopen", lambda *a, **k: _Resp())

    resp = asyncio.run(ea.realtime_call(_sdp_request(), authorization="Bearer t"))
    assert resp.media_type == "application/sdp"
    assert resp.body.decode() == answer
