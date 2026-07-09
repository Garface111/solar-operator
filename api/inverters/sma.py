"""SMA inverter source — Monitoring API (ennexOS / smaapis.de).

╔════════════════════════════════════════════════════════════════════════════╗
║ STATUS — auth + consent + discovery VERIFIED against the LIVE sandbox        ║
║ (2026-07-08, Custom-Flow / service-account creds). Blocked only on a real   ║
║ consented plant WITH generation data + the signed contract.                 ║
║                                                                            ║
║ VERIFIED against sandbox-auth.smaapis.de / sandbox.smaapis.de:              ║
║  • token: POST {AUTH_URL} grant_type=client_credentials → Bearer,           ║
║    scope monitoringApi:read, expires_in 300.                                ║
║  • consent: POST {BC_BASE}/oauth2/v2/bc-authorize  (Bearer + JSON           ║
║    {"loginHint": <email>}) → 201 {loginHint, state, expirationDate,          ║
║    interval}. Re-POSTing is how you READ current state (there is NO GET      ║
║    status endpoint — the {email}/status resource is PUT-only, used by the   ║
║    sandbox to SIMULATE the owner's approval). state ∈ Pending|Accepted|      ║
║    Revoked (SMA's async-auth enum; no explicit Rejected/Denied).            ║
║  • discovery: GET {MON_BASE}/plants → {"plants":[{plantId,name,timezone}]}. ║
║    Empty until an owner consents; after approval the owner's plants appear. ║
║    NO per-owner filter exists on /plants (loginHint/owner params ignored) — ║
║    the app token lists EVERY consented owner's plants, so connect-account    ║
║    must scope by explicit system_ids (see array_owners.sma_connect_account).║
║                                                                            ║
║ UNVERIFIED — pending a real plant with generation:                          ║
║  • the measurement VALUES. The envelope is confirmed                        ║
║    {plant, setType, resolution, set:[{time, pvGeneration}]} but sandbox      ║
║    test plants carry zero generation (`set` is always []), so the inner      ║
║    value units (W for Recent, Wh for Day) are parsed per SMA's OpenAPI       ║
║    schema, not observed. See HANDOFF_API_VERIFICATION.md.                   ║
╚════════════════════════════════════════════════════════════════════════════╝

OAuth2. Owner-facing config carries only {"system_id"}; the app-level
client_id/client_secret merge in from the environment at call time
(_resolve_creds). Legacy per-connection {client_id, client_secret,
refresh_token} still work as a fallback.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import httpx

from .base import TIMEOUT, InverterAuthError, InverterError, require_fields

CODE = "sma"
LABEL = "SMA (Sunny Portal / ennexOS)"
AVAILABLE = True
NOTE = "Connect by approving a one-time request in your SMA / Sunny Portal account — no keys."
SUPPORTS_LIVE = True
SUPPORTS_DAILY = True
# Owner-facing connect is the consent flow (owner enters only their email); the
# server discovers plants and stores {system_id} per connection. These FIELDS
# describe the LEGACY manual fallback (a plant owner who already holds their own
# SMA app credentials) — the default AO connect path is the email-consent flow
# in array_owners.sma_* and does not use them.
CONNECT_MODE = "consent"          # UI hint: render the email→approve flow, not a key form
CONSENT_EMAIL_LABEL = "Your SMA / Sunny Portal email"
FIELDS = [
    {"name": "system_id", "label": "Plant / System ID", "secret": False},
    {"name": "client_id", "label": "Client ID (advanced / legacy)", "secret": False,
     "optional": True},
    {"name": "client_secret", "label": "Client Secret (advanced / legacy)", "secret": True,
     "optional": True},
    {"name": "refresh_token", "label": "Refresh token (optional)", "secret": True,
     "optional": True},
]

# ── URL layout (single adjust-point; every host env-overridable) ──────────────
# ⚠️ PROD hosts are set via env on Railway (SMA_AUTH_URL / SMA_MON_BASE /
# SMA_BC_BASE) — these code defaults are only a fallback and follow SMA's
# published production hostnames. The VERIFIED sandbox values (proven 2026-07-08)
# were: AUTH=https://sandbox-auth.smaapis.de/oauth2/token,
# MON=https://sandbox.smaapis.de/monitoring/v1, BC=https://sandbox.smaapis.de.
# Sandbox puts the monitoring API under a "/monitoring" path on the shared host;
# production uses a dedicated monitoring.smaapis.de host with a "/v1" prefix.
AUTH_URL = os.environ.get("SMA_AUTH_URL", "https://auth.smaapis.de/oauth2/token")
MON_BASE = os.environ.get("SMA_MON_BASE", "https://monitoring.smaapis.de/v1")
# Backchannel (CIBA-style) consent: our ONE registered app asks SMA to prompt a
# plant OWNER (their Sunny Portal email) for consent; the owner approves inside
# their SMA account; then our app token can read their plants. VERIFIED shape:
# POST {BC_BASE}/oauth2/v2/bc-authorize with Bearer + JSON {"loginHint": email}.
# ⚠️ The consent gateway host DIFFERS between environments (confirmed against
# SMA's official API Access Control docs, 2026-07-09):
#   • Sandbox    → https://sandbox.smaapis.de  (bc-authorize sits at the root of
#     the shared sandbox host; it 404s on sandbox-auth.smaapis.de).
#   • Production → https://async-auth.smaapis.de  (a DEDICATED backchannel host,
#     distinct from BOTH auth.smaapis.de and monitoring.smaapis.de).
# So the production default below is async-auth; Railway overrides BC_BASE to the
# sandbox host while we're still in sandbox.
BC_BASE = os.environ.get("SMA_BC_BASE", "https://async-auth.smaapis.de")

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
    """Pull the pvGeneration value (+ time) from a measurement-set response.

    VERIFIED envelope (SMA Monitoring API, sandbox 2026-07-08 + OpenAPI schema):
        {"plant": {...}, "setType": "EnergyAndPowerPv", "resolution": "...",
         "set": [ {"time": "2020-03-23T12:40:00", "pvGeneration": 7732.648}, … ]}
    For period=Recent the `set` holds the latest sample (pvGeneration in W);
    for period=Day it holds daily aggregates (pvGeneration in Wh). We take the
    LAST non-null pvGeneration in the set (the freshest / the requested day).

    ⚠️ The `set` ITEM values are unverified against live generation — sandbox
    test plants return `set: []`. Parsing follows the OpenAPI schema. Two
    legacy/top-level shapes are tolerated too so a docs shift can't hard-fail.
    """
    body = body or {}
    rows = body.get("set")
    if isinstance(rows, list):
        val, ts = None, None
        for row in rows:
            if not isinstance(row, dict):
                continue
            pv = row.get("pvGeneration")
            if pv is not None:
                val, ts = pv, row.get("time") or row.get("timestamp")
        return val, ts
    # Tolerated fallbacks: {"pvGeneration": {"value","time"}} / {"pvGeneration": v}
    pv = body.get("pvGeneration")
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
# Production connect flow: tenant enters the plant OWNER's Sunny Portal email →
# request_consent() (POST bc-authorize) → SMA prompts the owner inside their
# account → consent_status() (re-POST bc-authorize, reads current state) flips to
# "accepted" → discover_systems() (GET /plants) lists the plants our app can now
# read → the connect-account cascade attaches them.
#
# VERIFIED against the live sandbox 2026-07-08:
#   POST {BC_BASE}/oauth2/v2/bc-authorize
#     auth:  Bearer <service-account token>          (NOT client creds in body)
#     body:  JSON {"loginHint": "<email>"}           (NOT form; field is loginHint)
#     resp:  201 {"loginHint", "state", "expirationDate", "interval"}
#            state ∈ Pending | Accepted | Revoked
#   Re-POSTing the SAME loginHint returns the CURRENT state — that is how we
#   poll (there is no GET status endpoint; {email}/status is PUT-only and used
#   by the sandbox to simulate the owner's approval, see _sandbox_simulate_*).

_BC_URL = f"{BC_BASE}/oauth2/v2/bc-authorize"


def _bc_authorize(email: str) -> dict:
    """POST bc-authorize for `email`; returns the parsed body. Used by BOTH
    request_consent (kick off) and consent_status (re-POST to read state) —
    SMA's backchannel endpoint is idempotent-per-email and returns the current
    state each time."""
    creds = _resolve_creds({})
    try:
        resp = httpx.post(
            _BC_URL,
            json={"loginHint": email, "scope": "monitoringApi:read"},
            headers={"Authorization": f"Bearer {_get_token(creds)}"},
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
        return resp.json() or {}
    except Exception:  # noqa: BLE001 — some 2xx responses may be body-less
        return {}


def _normalize_state(raw: str | None) -> str:
    """SMA's async-auth enum (Pending|Accepted|Revoked, capitalized) → our
    lowercase vocabulary. Unknown values map to 'unknown', never a crash."""
    s = str(raw or "").strip().lower()
    return s if s in ("pending", "accepted", "revoked", "rejected") else "unknown"


def request_consent(owner_email: str) -> dict:
    """Ask SMA to prompt `owner_email` for data-sharing consent with our app.

    Returns {"requested": True, "state": pending|accepted|revoked|unknown,
    "expiration": str|None}. Raises InverterAuthError when the app credentials
    are missing/rejected."""
    email = (owner_email or "").strip()
    if not email or "@" not in email:
        raise InverterError("A valid plant-owner email is required.")
    body = _bc_authorize(email)
    return {
        "requested": True,
        "state": _normalize_state(body.get("state")),
        "expiration": body.get("expirationDate"),
    }


def consent_status(owner_email: str) -> str:
    """The owner's current consent state, normalized to one of:
    pending | accepted | revoked | rejected | unknown.

    Reads state by RE-POSTing bc-authorize (SMA has no GET status endpoint);
    the response carries the live state. Re-posting does not spam the owner —
    once Accepted, SMA returns Accepted without re-prompting."""
    email = (owner_email or "").strip()
    if not email:
        return "unknown"
    body = _bc_authorize(email)
    return _normalize_state(body.get("state"))


def _sandbox_simulate_approval(owner_email: str, state: str = "Accepted") -> int:
    """SANDBOX-ONLY: simulate the owner approving/revoking via the PUT status
    resource (there is no such control in production — the owner approves inside
    Sunny Portal). Returns the HTTP status. Used by the verify harness/tests
    only; never called by the app flow."""
    creds = _resolve_creds({})
    email = (owner_email or "").strip()
    resp = httpx.put(
        f"{_BC_URL}/{email}/status",
        json={"state": state},
        headers={"Authorization": f"Bearer {_get_token(creds)}"},
        timeout=TIMEOUT,
    )
    return resp.status_code


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
