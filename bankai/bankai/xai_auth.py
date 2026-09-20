"""Resolve a Bearer token that bills Ford's **Grok Build prepaid team**.

Two xAI billing identities exist on this machine historically:

  1. console.x.ai classic API key (often credit-capped)
  2. Grok Build OIDC team ``41aa6b82-…`` (prepaid Build credits)

Grok Build CLI stores OIDC tokens in ``~/.grok/auth.json``. Hermes Agent
keeps a live refreshable OIDC session in ``~/.hermes/auth.json`` under the
``xai-oauth`` provider — same team, same ``api.x.ai`` bearer shape.

Priority (default: prefer Build OIDC so we never silently bill a capped key):

  1. Classic ``XAI_API_KEY`` (``xai-…``) only when ``XAI_PREFER_GROK_BUILD_OIDC=0``
  2. Cached / refreshed OIDC access token from env, ``~/.grok/auth.json``,
     or Hermes ``xai-oauth`` (whichever is freshest / refreshable)
  3. Last-resort classic key only if OIDC is not configured at all

Refresh tokens are single-use/rotating. Rotations are written back to the
store they came from (and mirrored into ``~/.grok/auth.json`` when the source
was Hermes) so Grok CLI + BankAI stay in sync.
"""
from __future__ import annotations

import base64
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

log = logging.getLogger("bankai.xai_auth")

_lock = threading.Lock()
_cache: dict[str, Any] = {
    "access_token": None,
    "expires_at": 0.0,
    "source": None,
}


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _env_api_key() -> str:
    return (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()


def _jwt_exp(tok: str) -> int | None:
    try:
        parts = (tok or "").split(".")
        if len(parts) < 2:
            return None
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad.encode()))
        exp = int(payload.get("exp") or 0)
        return exp or None
    except Exception:  # noqa: BLE001
        return None


def _jwt_claim(tok: str, claim: str) -> Any:
    try:
        parts = (tok or "").split(".")
        if len(parts) < 2:
            return None
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad.encode()))
        return payload.get(claim)
    except Exception:  # noqa: BLE001
        return None


def _grok_auth_path() -> Path:
    return Path(os.getenv("GROK_AUTH_JSON") or os.path.expanduser("~/.grok/auth.json"))


def _hermes_auth_path() -> Path:
    return Path(
        os.getenv("HERMES_AUTH_JSON") or os.path.expanduser("~/.hermes/auth.json")
    )


def _load_oidc_from_grok_auth() -> dict[str, Any] | None:
    p = _grok_auth_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("read %s failed: %s", p, e)
        return None
    if not isinstance(data, dict) or not data:
        return None
    # Prefer the entry with the newest create_time / furthest exp
    best_key = None
    best_entry = None
    best_score = -1.0
    for k, entry in data.items():
        if not isinstance(entry, dict):
            continue
        tok = (entry.get("key") or entry.get("access_token") or "").strip()
        exp = _jwt_exp(tok) or 0
        score = float(exp)
        if score >= best_score:
            best_score = score
            best_key = k
            best_entry = entry
    if not best_entry:
        return None
    return {
        "access_token": (
            best_entry.get("key") or best_entry.get("access_token") or ""
        ).strip(),
        "refresh_token": (best_entry.get("refresh_token") or "").strip(),
        "client_id": (
            best_entry.get("oidc_client_id")
            or os.getenv("XAI_OIDC_CLIENT_ID")
            or ""
        ).strip(),
        "token_url": (
            os.getenv("XAI_OIDC_TOKEN_URL")
            or f"{(best_entry.get('oidc_issuer') or 'https://auth.x.ai').rstrip('/')}/oauth2/token"
        ),
        "team_id": str(best_entry.get("team_id") or ""),
        "email": str(best_entry.get("email") or ""),
        "source": "grok_auth_json",
        "store_path": str(p),
        "store_key": best_key,
    }


def _load_oidc_from_hermes() -> dict[str, Any] | None:
    """Bootstrap from Hermes Agent's live xai-oauth session (same Build team)."""
    p = _hermes_auth_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("read %s failed: %s", p, e)
        return None
    if not isinstance(data, dict):
        return None

    access = refresh = ""
    # provider block
    prov = (data.get("providers") or {}).get("xai-oauth") or {}
    tokens = prov.get("tokens") if isinstance(prov, dict) else None
    if isinstance(tokens, dict):
        access = (tokens.get("access_token") or "").strip()
        refresh = (tokens.get("refresh_token") or "").strip()
    # credential pool may hold a fresher copy
    for cred in (data.get("credential_pool") or {}).get("xai-oauth") or []:
        if not isinstance(cred, dict):
            continue
        ca = (cred.get("access_token") or "").strip()
        cr = (cred.get("refresh_token") or "").strip()
        if ca and (_jwt_exp(ca) or 0) >= (_jwt_exp(access) or 0):
            access = ca or access
            refresh = cr or refresh

    if not access and not refresh:
        return None

    client_id = (
        os.getenv("XAI_OIDC_CLIENT_ID")
        or (_jwt_claim(access, "client_id") if access else None)
        or (_jwt_claim(access, "aud") if access else None)
        or ""
    )
    if isinstance(client_id, list):
        client_id = client_id[0] if client_id else ""
    client_id = str(client_id).strip()
    discovery = (prov.get("discovery") or {}) if isinstance(prov, dict) else {}
    token_url = (
        os.getenv("XAI_OIDC_TOKEN_URL")
        or discovery.get("token_endpoint")
        or "https://auth.x.ai/oauth2/token"
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "client_id": client_id,
        "token_url": str(token_url).strip(),
        "team_id": str(_jwt_claim(access, "team_id") or ""),
        "email": "",
        "source": "hermes_auth_json",
        "store_path": str(p),
        "store_key": "xai-oauth",
    }


def _env_oidc() -> dict[str, Any] | None:
    rt = (os.getenv("XAI_OIDC_REFRESH_TOKEN") or "").strip()
    cid = (os.getenv("XAI_OIDC_CLIENT_ID") or "").strip()
    at = (os.getenv("XAI_ACCESS_TOKEN") or "").strip()
    if not ((rt and cid) or at):
        return None
    return {
        "access_token": at,
        "refresh_token": rt,
        "client_id": cid,
        "token_url": (
            os.getenv("XAI_OIDC_TOKEN_URL") or "https://auth.x.ai/oauth2/token"
        ).strip(),
        "team_id": (os.getenv("XAI_OIDC_TEAM_ID") or "").strip(),
        "email": "",
        "source": "env",
        "store_path": None,
        "store_key": None,
    }


def _pick_oidc() -> dict[str, Any] | None:
    """Choose the OIDC config with the furthest-out access token (prefer refreshable)."""
    candidates = [
        c
        for c in (_env_oidc(), _load_oidc_from_grok_auth(), _load_oidc_from_hermes())
        if c
    ]
    if not candidates:
        return None

    def score(c: dict[str, Any]) -> tuple:
        exp = _jwt_exp(c.get("access_token") or "") or 0
        refreshable = 1 if (c.get("refresh_token") and c.get("client_id")) else 0
        # Prefer still-valid access; among those, prefer refreshable; then furthest exp
        valid = 1 if exp > time.time() + 60 else 0
        return (valid, refreshable, exp)

    return max(candidates, key=score)


def _write_back_tokens(cfg: dict[str, Any], access: str, refresh: str | None) -> None:
    """Persist rotated tokens so the next process sees them."""
    store = cfg.get("store_path")
    if not store:
        return
    path = Path(store)
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except Exception:  # noqa: BLE001
        data = {}

    if cfg.get("source") == "hermes_auth_json" and isinstance(data, dict):
        prov = (data.setdefault("providers", {})).setdefault("xai-oauth", {})
        tokens = prov.setdefault("tokens", {})
        if isinstance(tokens, dict):
            tokens["access_token"] = access
            if refresh:
                tokens["refresh_token"] = refresh
        pool = (data.get("credential_pool") or {}).get("xai-oauth") or []
        for cred in pool:
            if isinstance(cred, dict) and cred.get("access_token"):
                cred["access_token"] = access
                if refresh:
                    cred["refresh_token"] = refresh
        try:
            path.write_text(json.dumps(data, indent=2))
        except Exception as e:  # noqa: BLE001
            log.warning("failed writing hermes auth: %s", e)
        # Mirror into ~/.grok/auth.json so Grok CLI stays alive too
        _mirror_to_grok_auth(access, refresh, cfg)
        return

    if cfg.get("source") == "grok_auth_json" and isinstance(data, dict):
        key = cfg.get("store_key")
        entry = data.get(key) if key in data else None
        if not isinstance(entry, dict):
            # fall back to first entry
            for k, v in data.items():
                if isinstance(v, dict):
                    key, entry = k, v
                    break
        if isinstance(entry, dict) and key is not None:
            entry["key"] = access
            if refresh:
                entry["refresh_token"] = refresh
            exp = _jwt_exp(access)
            if exp:
                from datetime import datetime, timezone

                entry["expires_at"] = datetime.fromtimestamp(
                    exp, tz=timezone.utc
                ).isoformat()
            data[key] = entry
            try:
                path.write_text(json.dumps(data, indent=2))
            except Exception as e:  # noqa: BLE001
                log.warning("failed writing grok auth: %s", e)
        return


def _mirror_to_grok_auth(
    access: str, refresh: str | None, cfg: dict[str, Any]
) -> None:
    p = _grok_auth_path()
    try:
        data = json.loads(p.read_text()) if p.is_file() else {}
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    client_id = cfg.get("client_id") or ""
    entry_key = None
    entry = None
    for k, v in data.items():
        if isinstance(v, dict) and (
            v.get("oidc_client_id") == client_id
            or str(v.get("team_id") or "") == str(cfg.get("team_id") or "")
        ):
            entry_key, entry = k, v
            break
    if entry is None:
        # invent a stable key matching Grok CLI's shape
        entry_key = f"https://auth.x.ai::{client_id or 'bankai'}"
        entry = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://auth.x.ai",
            "oidc_client_id": client_id,
            "team_id": cfg.get("team_id") or "",
        }
    entry["key"] = access
    if refresh:
        entry["refresh_token"] = refresh
    exp = _jwt_exp(access)
    if exp:
        from datetime import datetime, timezone

        entry["expires_at"] = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    if cfg.get("team_id"):
        entry["team_id"] = cfg["team_id"]
    if client_id:
        entry["oidc_client_id"] = client_id
    data[entry_key] = entry
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
        os.chmod(p, 0o600)
    except Exception as e:  # noqa: BLE001
        log.warning("failed mirroring to %s: %s", p, e)


def _refresh_access_token(cfg: dict[str, Any]) -> str:
    if not cfg.get("refresh_token") or not cfg.get("client_id"):
        raise RuntimeError("oidc refresh missing refresh_token or client_id")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": cfg["refresh_token"],
            "client_id": cfg["client_id"],
        }
    ).encode()
    req = urllib.request.Request(
        cfg["token_url"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        resp = json.loads(r.read().decode())
    at = (resp.get("access_token") or "").strip()
    if not at:
        raise RuntimeError("oidc refresh returned no access_token")
    expires_in = int(resp.get("expires_in") or 21000)
    new_rt = (resp.get("refresh_token") or "").strip() or None
    with _lock:
        _cache["access_token"] = at
        _cache["expires_at"] = time.time() + max(60, expires_in - 120)
        _cache["source"] = f"oidc_refresh:{cfg.get('source')}"
    _write_back_tokens(cfg, at, new_rt)
    # Keep cfg itself current for any follow-up in this process
    cfg["access_token"] = at
    if new_rt:
        cfg["refresh_token"] = new_rt
    log.info(
        "xAI OIDC refreshed (source=%s team=%s expires_in=%ss)",
        cfg.get("source"),
        cfg.get("team_id") or "?",
        expires_in,
    )
    return at


def get_xai_bearer(*, force_refresh: bool = False) -> str:
    """Return a Bearer token for ``api.x.ai`` (Build OIDC preferred)."""
    prefer_oidc = _flag("XAI_PREFER_GROK_BUILD_OIDC", "1")
    api_key = _env_api_key()
    cfg = _pick_oidc()

    if api_key.startswith("xai-") and not prefer_oidc and not force_refresh:
        return api_key

    if cfg and (
        cfg.get("access_token")
        or (cfg.get("refresh_token") and cfg.get("client_id"))
    ):
        with _lock:
            tok = _cache.get("access_token")
            exp = float(_cache.get("expires_at") or 0)
        if not force_refresh and tok and time.time() < exp:
            return str(tok)

        access = (cfg.get("access_token") or "").strip()
        exp_jwt = _jwt_exp(access) or 0
        # Still-valid access token: use it, refresh proactively only if near expiry
        near_expiry = exp_jwt and exp_jwt < time.time() + 300
        if (
            not force_refresh
            and access
            and exp_jwt > time.time() + 60
            and not near_expiry
        ):
            with _lock:
                _cache["access_token"] = access
                _cache["expires_at"] = float(exp_jwt - 60)
                _cache["source"] = cfg.get("source")
            return access

        if cfg.get("refresh_token") and cfg.get("client_id"):
            try:
                return _refresh_access_token(cfg)
            except Exception as e:  # noqa: BLE001
                if access and exp_jwt > time.time() + 30:
                    log.warning("oidc refresh failed; using still-valid access: %s", e)
                    return access
                if access and not prefer_oidc:
                    log.warning("oidc refresh failed; stale access token: %s", e)
                    return access
                if not prefer_oidc and api_key:
                    log.warning("oidc failed; falling back to XAI_API_KEY: %s", e)
                    return api_key
                raise RuntimeError(
                    f"Grok Build OIDC failed ({e}). Re-run `grok login` "
                    "(or Hermes xAI OAuth) so ~/.grok/auth.json / "
                    "~/.hermes/auth.json hold a live refresh token."
                ) from e

        if access:
            return access

    if api_key and not prefer_oidc:
        return api_key
    if api_key and prefer_oidc and not cfg:
        return api_key
    if api_key and prefer_oidc and cfg:
        raise RuntimeError(
            "XAI_PREFER_GROK_BUILD_OIDC=1 but OIDC token missing/expired; "
            "refusing capped API key. Run `grok login` or refresh Hermes xAI OAuth."
        )
    raise RuntimeError(
        "no xAI credentials: set XAI_API_KEY, or sign in with `grok login`, "
        "or keep Hermes Agent on xai-oauth (BankAI will reuse that session)"
    )


def xai_auth_status() -> dict[str, Any]:
    """Diagnostics — no secrets."""
    api_key = _env_api_key()
    cfg = _pick_oidc() or {}
    with _lock:
        cached = bool(_cache.get("access_token"))
        exp = _cache.get("expires_at")
        source = _cache.get("source")
    access = (cfg.get("access_token") or "").strip()
    return {
        "api_key_present": bool(api_key),
        "api_key_is_classic": api_key.startswith("xai-") if api_key else False,
        "oidc_configured": bool(cfg.get("refresh_token") and cfg.get("client_id")),
        "oidc_source": cfg.get("source"),
        "oidc_team_id": cfg.get("team_id") or None,
        "oidc_email": cfg.get("email") or None,
        "access_seconds_left": max(0, (_jwt_exp(access) or 0) - int(time.time()))
        if access
        else None,
        "prefer_grok_build_oidc": _flag("XAI_PREFER_GROK_BUILD_OIDC", "1"),
        "cached_access_token": cached,
        "cache_expires_at": exp,
        "cache_source": source,
        "grok_auth_exists": _grok_auth_path().is_file(),
        "hermes_auth_exists": _hermes_auth_path().is_file(),
    }
