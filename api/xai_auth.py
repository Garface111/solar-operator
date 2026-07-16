"""xAI / Grok bearer resolution for Energy Agent + Sovereign.

Two billing identities exist for Ford:
  1. console.x.ai **API key** team (was credit-capped: a2f4ee20-…)
  2. **Grok Build** OIDC team (has prepaid credits: 41aa6b82-…)

Grok Build CLI stores OIDC tokens in ~/.grok/auth.json. Those JWTs work as
Bearer tokens on https://api.x.ai/v1/chat/completions and bill the Build team.

Priority:
  1. XAI_API_KEY if it is a classic key (xai-…) and XAI_FORCE_OIDC is off
  2. Cached / refreshed Grok Build OIDC access token (env or ~/.grok/auth.json)
  3. Fall back to XAI_API_KEY even if JWT (manual paste)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("xai_auth")

_lock = threading.Lock()
_cache: dict[str, Any] = {
    "access_token": None,
    "expires_at": 0.0,  # unix
    "source": None,
}


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _env_api_key() -> str:
    return (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()


def _auth_json_path() -> Path:
    return Path(os.getenv("GROK_AUTH_JSON") or os.path.expanduser("~/.grok/auth.json"))


def _load_oidc_from_auth_json() -> dict[str, str] | None:
    p = _auth_json_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("read %s failed: %s", p, e)
        return None
    if not isinstance(data, dict) or not data:
        return None
    # Prefer newest entry
    entries = list(data.values())
    entry = entries[0]
    if not isinstance(entry, dict):
        return None
    return {
        "access_token": (entry.get("key") or entry.get("access_token") or "").strip(),
        "refresh_token": (entry.get("refresh_token") or "").strip(),
        "client_id": (entry.get("oidc_client_id") or os.getenv("XAI_OIDC_CLIENT_ID") or "").strip(),
        "token_url": (
            os.getenv("XAI_OIDC_TOKEN_URL")
            or f"{(entry.get('oidc_issuer') or 'https://auth.x.ai').rstrip('/')}/oauth2/token"
        ),
        "team_id": str(entry.get("team_id") or ""),
        "email": str(entry.get("email") or ""),
    }


def _oidc_config() -> dict[str, str] | None:
    """Env-first (Railway), then ~/.grok/auth.json (local / Grok Build host)."""
    rt = (os.getenv("XAI_OIDC_REFRESH_TOKEN") or "").strip()
    cid = (os.getenv("XAI_OIDC_CLIENT_ID") or "").strip()
    if rt and cid:
        return {
            "access_token": (os.getenv("XAI_ACCESS_TOKEN") or "").strip(),
            "refresh_token": rt,
            "client_id": cid,
            "token_url": (
                os.getenv("XAI_OIDC_TOKEN_URL") or "https://auth.x.ai/oauth2/token"
            ).strip(),
            "team_id": (os.getenv("XAI_OIDC_TEAM_ID") or "").strip(),
            "email": "",
        }
    return _load_oidc_from_auth_json()


def _refresh_access_token(cfg: dict[str, str]) -> str:
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
        "client_id": cfg["client_id"],
    }).encode()
    req = urllib.request.Request(
        cfg["token_url"],
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        body = json.loads(r.read().decode())
    at = (body.get("access_token") or "").strip()
    if not at:
        raise RuntimeError("oidc refresh returned no access_token")
    expires_in = int(body.get("expires_in") or 21000)
    # Persist rotated refresh token if provided
    new_rt = (body.get("refresh_token") or "").strip()
    with _lock:
        _cache["access_token"] = at
        _cache["expires_at"] = time.time() + max(60, expires_in - 120)
        _cache["source"] = "oidc_refresh"
        if new_rt:
            _cache["refresh_token"] = new_rt
    log.info(
        "xAI OIDC refreshed (team=%s, expires_in=%ss)",
        cfg.get("team_id") or "?",
        expires_in,
    )
    return at


def get_xai_bearer(*, force_refresh: bool = False) -> str:
    """Return a Bearer token that can call api.x.ai (API key or Build OIDC)."""
    prefer_oidc = _flag("XAI_PREFER_GROK_BUILD_OIDC", "1")
    api_key = _env_api_key()
    cfg = _oidc_config()

    # Fast path: classic API key when not forcing OIDC / no OIDC available
    if api_key.startswith("xai-") and not prefer_oidc and not force_refresh:
        return api_key

    # OIDC path (Grok Build subscription credits)
    if cfg and cfg.get("refresh_token") and cfg.get("client_id"):
        with _lock:
            tok = _cache.get("access_token")
            exp = float(_cache.get("expires_at") or 0)
        if not force_refresh and tok and time.time() < exp:
            return str(tok)
        # Prefer fresh access token from auth.json if still valid-looking
        if not force_refresh and cfg.get("access_token") and len(cfg["access_token"]) > 40:
            # use once; refresh in background-ish next call if expired
            try:
                return _refresh_access_token(cfg)
            except Exception as e:  # noqa: BLE001
                log.warning("oidc refresh failed, trying cached access_token: %s", e)
                if cfg.get("access_token"):
                    return cfg["access_token"]
                raise
        return _refresh_access_token(cfg)

    if api_key:
        return api_key
    raise RuntimeError(
        "no xAI credentials: set XAI_API_KEY (console.x.ai key on the team with credits) "
        "or XAI_OIDC_REFRESH_TOKEN + XAI_OIDC_CLIENT_ID (Grok Build), "
        "or sign in with `grok login` on this host"
    )


def xai_auth_status() -> dict[str, Any]:
    """Diagnostics for /health or admin — no secrets."""
    api_key = _env_api_key()
    cfg = _oidc_config()
    with _lock:
        cached = bool(_cache.get("access_token"))
        exp = _cache.get("expires_at")
    return {
        "api_key_present": bool(api_key),
        "api_key_is_classic": api_key.startswith("xai-") if api_key else False,
        "oidc_configured": bool(cfg and cfg.get("refresh_token") and cfg.get("client_id")),
        "oidc_team_id": (cfg or {}).get("team_id") or None,
        "oidc_email": (cfg or {}).get("email") or None,
        "prefer_grok_build_oidc": _flag("XAI_PREFER_GROK_BUILD_OIDC", "1"),
        "cached_access_token": cached,
        "cache_expires_at": exp,
        "auth_json_path": str(_auth_json_path()),
        "auth_json_exists": _auth_json_path().is_file(),
    }
