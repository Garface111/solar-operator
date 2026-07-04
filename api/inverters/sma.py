"""SMA inverter source — Monitoring API (ennexOS / smaapis.de).

╔════════════════════════════════════════════════════════════════════════════╗
║ LOUD CAVEAT — UNVERIFIED AGAINST A LIVE SMA ACCOUNT.                        ║
║                                                                            ║
║ This adapter requires an app registration with SMA (client_id / client_    ║
║ secret issued by the SMA developer portal) AND a plant-owner consent flow. ║
║ The endpoints below follow SMA's PUBLISHED docs but have NOT been run       ║
║ against a real account/token — treat the response parsing as best-effort    ║
║ until a live SMA system confirms the exact JSON shapes.                     ║
╚════════════════════════════════════════════════════════════════════════════╝

OAuth2. Config: {"client_id", "client_secret", "system_id", "refresh_token"?}.
client_credentials grant when no refresh_token is supplied, else refresh_token
grant. Tokens are cached per client_id with their expiry.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "sma"
LABEL = "SMA (Sunny Portal / ennexOS)"
AVAILABLE = True
NOTE = "Requires SMA developer app registration + owner consent. Endpoints unverified."
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
FIELDS = [
    {"name": "client_id", "label": "Client ID", "secret": False},
    {"name": "client_secret", "label": "Client Secret", "secret": True},
    {"name": "system_id", "label": "Plant / System ID", "secret": False},
    {"name": "refresh_token", "label": "Refresh token (optional)", "secret": True},
]

# ── URL layout (single adjust-point for the sandbox verification run) ─────────
# Production defaults per SMA's published docs; every one env-overridable so
# the sandbox run (SMA_SANDBOX=1 in scripts/verify_inverter_apis) or a docs
# correction never needs a code change.
AUTH_URL = os.environ.get("SMA_AUTH_URL", "https://auth.smaapis.de/oauth2/token")
MON_BASE = os.environ.get("SMA_MON_BASE", "https://monitoring.smaapis.de/v1")
# Backchannel (CIBA-style) consent endpoints: our ONE registered app asks SMA to
# prompt a plant OWNER (their Sunny Portal email) for consent; the owner
# approves inside their SMA account; then our app token can read their plants.
# ⚠️ SHAPES UNVERIFIED until the sandbox run — paths follow SMA's published docs
# (POST …/oauth2/v2/bc-authorize, status at …/bc-authorize/{email}/status).
BC_BASE = os.environ.get("SMA_BC_BASE", "https://auth.smaapis.de")

# ── App-level credentials (the ONE registered EnergyAgent app) ────────────────
# SMA's model is per-app registration + per-owner consent, so client_id/secret
# are OURS (Railway env), never per-tenant. Per-connection configs then need
# only {system_id}; _resolve_creds merges the app creds in at call time.
_APP_ID_ENV = "SMA_APP_CLIENT_ID"
_APP_SECRET_ENV = "SMA_APP_CLIENT_SECRET"


def app_credentials() -> dict | None:
    """{client_id, client_secret} for the registered EnergyAgent app, or None
    until SMA approves the registration and the env vars are set."""
    cid = (os.environ.get(_APP_ID_ENV) or "").strip()
    sec = (os.environ.get(_APP_SECRET_ENV) or "").strip()
    if cid and sec:
        return {"client_id": cid, "client_secret": sec}
    return None


def is_app_configured() -> bool:
    return app_credentials() is not None


def _resolve_creds(config: dict) -> dict:
    """Config with client credentials guaranteed: per-connection creds win
    (legacy connections that stored their own), else the app-level env creds."""
    if config.get("client_id") and config.get("client_secret"):
        return config
    app = app_credentials()
    if app is None:
        raise InverterAuthError(
            "SMA app credentials are not configured (set SMA_APP_CLIENT_ID / "
            "SMA_APP_CLIENT_SECRET once SMA approves the app registration)."
        )
    merged = dict(config)
    merged["client_id"] = app["client_id"]
    merged["client_secret"] = app["client_secret"]
    return merged

# Token cache: client_id -> (access_token, refresh_token, expires_at). Module-
# scoped so a daily poll across many plants under one app reuses a single token.
# The refresh_token is cached too because SMA ROTATES it on every refresh grant —
# the response hands back a NEW refresh_token and invalidates the one just used.
# We must reuse the freshest one (and persist it back to the connection config,
# see _get_token's mutation of `config`) or the next refresh fails 401 and the
# plant goes dark until the owner manually reconnects.
_TOKEN_CACHE: dict[str, tuple[str, str | None, datetime]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _get_token(config: dict) -> str:
    cid = str(config["client_id"])
    cached = _TOKEN_CACHE.get(cid)
    if cached is not None and cached[2] > _now():
        return cached[0]

    # Prefer the freshest refresh_token we hold: the rotated one from the cache
    # (set by a prior refresh) over the original stored in config.
    refresh_token = (cached[1] if cached is not None else None) or config.get("refresh_token")

    if refresh_token:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cid,
            "client_secret": config["client_secret"],
        }
    else:
        data = {
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": config["client_secret"],
        }

    try:
        resp = httpx.post(AUTH_URL, data=data, timeout=TIMEOUT)
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SMA OAuth: {exc}") from exc
    if resp.status_code in (401, 403):
        # A rotated/expired refresh token lands here. Drop the dead cache entry
        # AND clear it from config so the NEXT call falls back to a fresh
        # client_credentials grant instead of retrying the dead token forever.
        _TOKEN_CACHE.pop(cid, None)
        if config.get("refresh_token"):
            config["refresh_token"] = None
        raise InverterAuthError("SMA OAuth rejected the credentials (401/403).")
    if not resp.is_success:
        raise InverterError(
            f"SMA token endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"SMA token endpoint returned non-JSON: {exc}") from exc

    token = body.get("access_token")
    if not token:
        raise InverterError("SMA token endpoint returned no access_token")
    ttl = int(body.get("expires_in") or 3600)
    # Capture the ROTATED refresh_token SMA just issued (falls back to the one we
    # sent if the response omits it). Cache it AND write it back into `config` in
    # place so the caller (poller) can persist it to the DB — surviving both
    # access-token expiry and a server redeploy that clears the in-memory cache.
    new_refresh = body.get("refresh_token") or refresh_token
    _TOKEN_CACHE[cid] = (token, new_refresh, _now() + timedelta(seconds=max(ttl - 60, 60)))
    if new_refresh and new_refresh != config.get("refresh_token"):
        config["refresh_token"] = new_refresh
    return token


def _get(config: dict, path: str, params: dict | None = None) -> dict:
    token = _get_token(config)
    try:
        resp = httpx.get(
            f"{MON_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SMA Monitoring: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("SMA Monitoring rejected the token (401/403).")
    if not resp.is_success:
        raise InverterError(
            f"SMA Monitoring {path} returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise InverterError(f"SMA Monitoring returned non-JSON response: {exc}") from exc


def _pv_generation(body: dict) -> tuple[float | None, str | None]:
    """Pull the pvGeneration measurement value (+ time) from a measurement set.

    Tolerates {"pvGeneration": {"value", "time"}} and {"pvGeneration": value}.
    """
    pv = (body or {}).get("pvGeneration")
    if isinstance(pv, dict):
        return pv.get("value"), pv.get("time") or pv.get("timestamp")
    return pv, None


def validate(config: dict) -> dict:
    require_fields(config, "system_id")
    config = _resolve_creds(config)
    body = _get(config, f"/plants/{config['system_id']}")
    return {"site_name": body.get("name")}


def fetch_live(config: dict) -> dict | None:
    require_fields(config, "system_id")
    config = _resolve_creds(config)
    body = _get(
        config,
        f"/plants/{config['system_id']}/measurements/sets/EnergyAndPowerPv/Recent",
    )
    value, as_of = _pv_generation(body)
    power_w = float(value) if value is not None else None
    return {"current_power_w": power_w, "as_of": as_of}


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    require_fields(config, "system_id")
    config = _resolve_creds(config)
    out: list[dict] = []
    day = start
    # Cap the per-call loop so a wide range can't fan out unbounded.
    for _ in range(90):
        if day > end:
            break
        body = _get(
            config,
            f"/plants/{config['system_id']}/measurements/sets/EnergyAndPowerPv/Day",
            params={"Date": day.isoformat()},
        )
        wh, _ts = _pv_generation(body)
        if wh is not None:
            out.append({"day": day, "kwh": float(wh) / 1000.0})
        day += timedelta(days=1)
    return out


# ── Owner consent (backchannel authorize) + plant discovery ────────────────────
# The production connect flow: tenant enters the plant OWNER's Sunny Portal
# email → request_consent() → SMA prompts the owner inside their account →
# consent_status() flips to "accepted" → discover_systems() lists the plants our
# app can now read → the connect-account cascade attaches them.
# ⚠️ Request/response SHAPES UNVERIFIED until the sandbox run (see BC_BASE note).
# Everything here parses defensively and normalizes to small stable dicts so a
# field-name correction after the sandbox run stays inside this module.

def request_consent(owner_email: str) -> dict:
    """Ask SMA to prompt `owner_email` for data-sharing consent with our app.

    Returns {"requested": True, "auth_req_id": str|None}. Raises
    InverterAuthError when the app credentials are missing/rejected."""
    creds = _resolve_creds({})
    email = (owner_email or "").strip()
    if not email or "@" not in email:
        raise InverterError("A valid plant-owner email is required.")
    try:
        resp = httpx.post(
            f"{BC_BASE}/oauth2/v2/bc-authorize",
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "login_hint": email,
                "scope": "monitoringApi:read",
            },
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SMA consent API: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("SMA rejected the app credentials (401/403).")
    if not resp.is_success:
        raise InverterError(
            f"SMA bc-authorize returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — some 2xx responses may be body-less
        body = {}
    return {"requested": True, "auth_req_id": body.get("auth_req_id")}


def consent_status(owner_email: str) -> str:
    """The owner's consent state, normalized to one of:
    pending | accepted | rejected | revoked | unknown."""
    creds = _resolve_creds({})
    email = (owner_email or "").strip()
    try:
        resp = httpx.get(
            f"{BC_BASE}/oauth2/v2/bc-authorize/{email}/status",
            headers={"Authorization": f"Bearer {_get_token(creds)}"},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise InverterError(f"Network error contacting SMA consent API: {exc}") from exc
    if resp.status_code in (401, 403):
        raise InverterAuthError("SMA rejected the app credentials (401/403).")
    if resp.status_code == 404:
        return "unknown"                      # no request on file for this email
    if not resp.is_success:
        raise InverterError(
            f"SMA consent status returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        raw = str((resp.json() or {}).get("status") or "").strip().lower()
    except Exception:  # noqa: BLE001
        raw = ""
    return raw if raw in ("pending", "accepted", "rejected", "revoked") else "unknown"


def discover_systems(config: dict | None = None) -> list[dict]:
    """List every plant our app token can currently read (GET /plants,
    paginated) — i.e. the union of all owners who granted consent. Returns
    [{system_id (str), name}]. The caller scopes selection to the owner the
    tenant just onboarded (SMA's listing carries no per-owner filter we can
    rely on until the sandbox run pins the shape)."""
    creds = _resolve_creds(dict(config or {}))
    out: list[dict] = []
    offset = 0
    limit = 50
    for _page in range(40):                   # hard cap — never walk forever
        body = _get(creds, "/plants", params={"offset": offset, "limit": limit})
        plants = body.get("plants") or body.get("items") or []
        for p in plants:
            sid = p.get("plantId") or p.get("systemId") or p.get("id")
            if not sid:
                continue
            out.append({
                "system_id": str(sid),
                "name": (p.get("name") or "").strip() or f"SMA plant {sid}",
            })
        if len(plants) < limit:
            break
        offset += limit
    return out
