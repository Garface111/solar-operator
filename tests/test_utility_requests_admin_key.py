"""Regression: non-ASCII admin keys must 403, not TypeError 500.

hmac.compare_digest(str, str) raises
  TypeError: comparing strings with non-ASCII characters is not supported
when either side has non-ASCII — scanners hit /v1/utility-requests with
weird keys and Sentry logged the 500 (culprit: list_requests → _check_admin).
"""
import api.utility_requests as urmod


def test_list_requests_non_ascii_key_is_403_not_500(client, monkeypatch):
    monkeypatch.setattr(urmod, "ADMIN_API_KEY", "ascii-secret-key")
    # Non-ASCII in query param (same TypeError path as header)
    r = client.get("/v1/utility-requests?key=caf%C3%A9-not-the-key")
    assert r.status_code == 403
    assert r.status_code != 500


def test_check_admin_non_ascii_provided_key_is_403(monkeypatch):
    """Direct unit path: header/query values with non-ASCII must not TypeError."""
    monkeypatch.setattr(urmod, "ADMIN_API_KEY", "ascii-secret-key")
    from fastapi import HTTPException

    try:
        urmod._check_admin("ключ-неверный", None)
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403
    try:
        urmod._check_admin(None, "café-wrong")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403


def test_list_requests_valid_key_still_works(client, monkeypatch):
    monkeypatch.setattr(urmod, "ADMIN_API_KEY", "ascii-secret-key")
    r = client.get("/v1/utility-requests?key=ascii-secret-key")
    assert r.status_code == 200
    body = r.json()
    assert "requests" in body


def test_check_admin_accepts_matching_non_ascii_secret(monkeypatch):
    """If ADMIN_API_KEY itself is non-ASCII, matching still works (bytes path)."""
    monkeypatch.setattr(urmod, "ADMIN_API_KEY", "sècret-🔑")
    urmod._check_admin("sècret-🔑", None)  # must not raise
    try:
        urmod._check_admin("wrong", None)
        raised = False
    except Exception as e:
        raised = True
        from fastapi import HTTPException
        assert isinstance(e, HTTPException) and e.status_code == 403
    assert raised
