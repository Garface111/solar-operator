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


# ── Self-healing token store ────────────────────────────────────────────────
# Grok Build OIDC refresh tokens are single-use / rotating. A rotated token that
# only lives in env (Railway) goes stale on the next refresh, and the brain dies
# when the access JWT expires. Persist rotations to the shared DB so web+worker
# and future restarts always read the freshest token. All best-effort: any DB
# failure silently falls back to env, i.e. exactly the pre-existing behaviour.
_KV_REFRESH = "sys_xai_oidc_refresh_token"
_KV_ACCESS = "sys_xai_oidc_access_token"


def _kv_get(key: str) -> str:
    try:
        from api.db import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            row = db.execute(
                text("SELECT value FROM ea_sovereign_memory WHERE key = :k"),
                {"k": key},
            ).fetchone()
            return (row[0] or "").strip() if row and row[0] else ""
    except Exception:  # noqa: BLE001 — never let token storage break auth
        return ""


def _kv_set(key: str, value: str) -> None:
    value = (value or "").strip()
    if not value:
        return
    try:
        from api.db import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(
                text(
                    "INSERT INTO ea_sovereign_memory (key, value, source, updated_at) "
                    "VALUES (:k, :v, 'xai_auth', now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = :v, "
                    "source = 'xai_auth', updated_at = now()"
                ),
                {"k": key, "v": value},
            )
            db.commit()
    except Exception:  # noqa: BLE001
        pass


def seed_oidc_tokens(refresh_token: str = "", access_token: str = "") -> dict:
    """Seed the shared token store after a fresh `grok login`. Clears then sets
    so a new login always wins over a rotated-then-revoked value."""
    if refresh_token:
        _kv_set(_KV_REFRESH, refresh_token)
    if access_token:
        _kv_set(_KV_ACCESS, access_token)
    with _lock:
        _cache["access_token"] = None
        _cache["expires_at"] = 0.0
    return {"seeded_refresh": bool(refresh_token), "seeded_access": bool(access_token)}


def _jwt_exp(tok: str) -> int | None:
    """Best-effort exp (unix secs) from a JWT access token, without verifying."""
    try:
        import base64
        parts = (tok or "").split(".")
        if len(parts) < 2:
            return None
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad.encode()))
        exp = int(payload.get("exp") or 0)
        return exp or None
    except Exception:  # noqa: BLE001
        return None


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
    # Prefer the self-healing DB store (freshest rotated token) over env seed.
    rt = _kv_get(_KV_REFRESH) or (os.getenv("XAI_OIDC_REFRESH_TOKEN") or "").strip()
    cid = (os.getenv("XAI_OIDC_CLIENT_ID") or "").strip()
    if rt and cid:
        return {
            "access_token": _kv_get(_KV_ACCESS) or (os.getenv("XAI_ACCESS_TOKEN") or "").strip(),
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
    # Persist rotation so the next process/restart doesn't reuse a revoked token.
    _kv_set(_KV_ACCESS, at)
    if new_rt:
        _kv_set(_KV_REFRESH, new_rt)
    log.info(
        "xAI OIDC refreshed (team=%s, expires_in=%ss)",
        cfg.get("team_id") or "?",
        expires_in,
    )
    return at


def get_xai_bearer(*, force_refresh: bool = False) -> str:
    """Return a Bearer token that can call api.x.ai (API key or Build OIDC).

    When XAI_PREFER_GROK_BUILD_OIDC=1 (default), never fall back to a classic
    xai- API key that might bill a different, credit-capped team.
    """
    prefer_oidc = _flag("XAI_PREFER_GROK_BUILD_OIDC", "1")
    api_key = _env_api_key()
    cfg = _oidc_config()

    # Fast path: classic API key only when OIDC is not preferred
    if api_key.startswith("xai-") and not prefer_oidc and not force_refresh:
        return api_key

    # OIDC path (Grok Build subscription / prepaid team)
    if cfg and (cfg.get("access_token") or (cfg.get("refresh_token") and cfg.get("client_id"))):
        with _lock:
            tok = _cache.get("access_token")
            exp = float(_cache.get("expires_at") or 0)
        if not force_refresh and tok and time.time() < exp:
            return str(tok)

        access = (cfg.get("access_token") or "").strip()
        # 1) Use provided access token first (Railway XAI_ACCESS_TOKEN / auth.json)
        if not force_refresh and len(access) > 40:
            with _lock:
                _cache["access_token"] = access
                # JWT exp unknown — cache briefly; refresh path extends it
                _cache["expires_at"] = time.time() + int(
                    os.getenv("XAI_ACCESS_TOKEN_TTL", "1800") or 1800
                )
                _cache["source"] = "access_token_env"
            # Proactive refresh if we have refresh_token (best-effort)
            if cfg.get("refresh_token") and cfg.get("client_id"):
                try:
                    return _refresh_access_token(cfg)
                except Exception as e:  # noqa: BLE001
                    log.warning("oidc refresh failed; using access token: %s", e)
                    return access
            return access

        # 2) Refresh only
        if cfg.get("refresh_token") and cfg.get("client_id"):
            try:
                return _refresh_access_token(cfg)
            except Exception as e:  # noqa: BLE001
                if access:
                    log.warning("oidc refresh failed; stale access token: %s", e)
                    return access
                if not prefer_oidc and api_key:
                    log.warning("oidc failed; falling back to XAI_API_KEY: %s", e)
                    return api_key
                raise RuntimeError(
                    f"Grok Build OIDC failed ({e}). Re-run `grok login` and update "
                    "XAI_OIDC_REFRESH_TOKEN / XAI_ACCESS_TOKEN on Railway."
                ) from e

    if api_key and not prefer_oidc:
        return api_key
    if api_key and prefer_oidc:
        # Last resort only if OIDC not configured at all
        if not cfg:
            return api_key
        raise RuntimeError(
            "XAI_PREFER_GROK_BUILD_OIDC=1 but OIDC token missing/expired; "
            "refusing capped API key. Update XAI_ACCESS_TOKEN or re-login Grok Build."
        )
    raise RuntimeError(
        "no xAI credentials: set XAI_API_KEY (console key on the team with credits) "
        "or XAI_OIDC_REFRESH_TOKEN + XAI_ACCESS_TOKEN (Grok Build), "
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
        "store": {
            "db_refresh": bool(_kv_get(_KV_REFRESH)),
            "db_access": bool(_kv_get(_KV_ACCESS)),
        },
    }


def token_info() -> dict[str, Any]:
    """Non-billing fuel view of the active bearer: kind, team, expiry countdown.

    Does not mint or refresh (no network) — reads the cached/config token only,
    so the Portal can poll it cheaply as a live fuel gauge.
    """
    prefer = _flag("XAI_PREFER_GROK_BUILD_OIDC", "1")
    info: dict[str, Any] = {
        "kind": None,
        "credits": None,
        "team_id": None,
        "prefer_build_oidc": prefer,
        "expires_at": None,
        "seconds_left": None,
        "source": None,
    }
    try:
        cfg = _oidc_config() or {}
        info["team_id"] = cfg.get("team_id") or None
        with _lock:
            tok = _cache.get("access_token")
            info["source"] = _cache.get("source")
        tok = (tok or cfg.get("access_token") or "").strip()
        api_key = _env_api_key()
        if tok and not tok.startswith("xai-"):
            info["kind"] = "grok_build_oidc"
            info["credits"] = "grok_build_prepaid"
            exp = _jwt_exp(tok)
            if exp:
                info["expires_at"] = exp
                info["seconds_left"] = max(0, exp - int(time.time()))
        elif (tok.startswith("xai-") or api_key) and not prefer:
            info["kind"] = "classic_api_key"
            info["credits"] = "console_api_capped"
        elif api_key and prefer:
            # prefer OIDC but only a classic key resolved → will refuse to bill it
            info["kind"] = "classic_api_key_blocked"
            info["credits"] = "refused_capped"
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)[:160]
    return info
