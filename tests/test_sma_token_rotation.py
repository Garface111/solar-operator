"""SMA OAuth refresh-token ROTATION handling (api/inverters/sma.py).

Regression for the 'SMA worked until I reconnected' bug: SMA rotates the
refresh_token on every refresh grant and invalidates the old one. The adapter
must (a) reuse the rotated token on the next refresh and (b) write it back into
the connection config so it survives access-token expiry + a redeploy.
"""
import importlib
from datetime import timedelta

import api.inverters.sma as sma


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.is_success = 200 <= status < 300
        self.text = str(body)

    def json(self):
        return self._body


def _reset():
    sma._TOKEN_CACHE.clear()


def test_rotation_reused_and_persisted(monkeypatch):
    _reset()
    # Each call returns a NEW access + refresh token and records what was sent.
    sent = []
    issued = {"n": 0}

    def fake_post(url, data=None, timeout=None):
        sent.append(dict(data or {}))
        issued["n"] += 1
        n = issued["n"]
        return _Resp(200, {
            "access_token": f"access-{n}",
            "refresh_token": f"refresh-{n}",
            "expires_in": 3600,
        })

    monkeypatch.setattr(sma.httpx, "post", fake_post)

    cfg = {"client_id": "cid1", "client_secret": "sec", "system_id": "p1",
           "refresh_token": "refresh-0"}

    # 1st token: uses the ORIGINAL stored refresh token.
    t1 = sma._get_token(cfg)
    assert t1 == "access-1"
    assert sent[-1]["grant_type"] == "refresh_token"
    assert sent[-1]["refresh_token"] == "refresh-0"
    # The rotated token must be written back into config.
    assert cfg["refresh_token"] == "refresh-1"

    # Force the access token to look expired so the next call refreshes again.
    tok, refresh, _exp = sma._TOKEN_CACHE["cid1"]
    sma._TOKEN_CACHE["cid1"] = (tok, refresh, sma._now() - timedelta(seconds=1))

    # 2nd token: must use the ROTATED token (refresh-1), NOT the dead refresh-0.
    t2 = sma._get_token(cfg)
    assert t2 == "access-2"
    assert sent[-1]["refresh_token"] == "refresh-1"   # the bug: would have re-sent refresh-0
    assert cfg["refresh_token"] == "refresh-2"


def test_dead_refresh_token_is_cleared(monkeypatch):
    _reset()

    def fake_post(url, data=None, timeout=None):
        return _Resp(401, "invalid_grant")

    monkeypatch.setattr(sma.httpx, "post", fake_post)
    cfg = {"client_id": "cid2", "client_secret": "sec", "system_id": "p1",
           "refresh_token": "dead-token"}
    try:
        sma._get_token(cfg)
        assert False, "expected InverterAuthError"
    except sma.InverterAuthError:
        pass
    # The dead token must be cleared from config so the next call can fall back
    # to a client_credentials grant instead of retrying a known-bad token.
    assert cfg["refresh_token"] is None
    assert "cid2" not in sma._TOKEN_CACHE
