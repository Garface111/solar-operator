"""Error monitoring — optional Sentry integration (June 2026, launch readiness).

Design goals:
  * ZERO behavior change unless SENTRY_DSN is set. No DSN → init_sentry() is a
    silent no-op and is_enabled() is False. This keeps local dev and the test
    suite completely unaffected, and means shipping the dependency can never
    surprise prod without explicit configuration.
  * Defense in depth: even if Sentry is down or unconfigured, the app's global
    exception handler still emails an internal alert (see app.py), so a 500 in
    prod is never fully silent.
  * Scrub PII / secrets before anything leaves the process — we never want a
    bearer token, password, or Stripe key in an error report.

Env:
  SENTRY_DSN              — the project DSN (absent = disabled)
  SENTRY_ENVIRONMENT      — e.g. "production" (default) / "staging"
  SENTRY_TRACES_SAMPLE_RATE — perf tracing sample rate, default 0.0 (errors only)
  SENTRY_RELEASE          — optional release/version tag
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("solar_operator.observability")

_ENABLED = False

# Header / field names whose values must never leave the process.
_SENSITIVE_KEYS = {
    "authorization", "cookie", "set-cookie", "x-api-key", "api-key",
    "password", "current_password", "session_token", "access_token",
    "id_token", "refresh_token", "tenant_key", "stripe-signature",
    "secret", "client_secret", "api_token", "apitoken", "x-admin-key",
    "x-seed-token", "x-maint-key", "so_config_key", "secret_enc",
    "session_state_enc", "raw_payload", "solaredge_api_key",
    # Request bodies / dataclasses that embed passwords in repr()
    "body", "creds", "credential", "credential_in", "password_hash",
}


def is_enabled() -> bool:
    """True only when Sentry was actually initialized (DSN present + SDK importable)."""
    return _ENABLED


def _scrub(data: Any) -> Any:
    """Recursively redact sensitive values in dict/list structures."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                out[k] = "[redacted]"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(data, (list, tuple)):
        return type(data)(_scrub(v) for v in data)
    return data


def _scrub_stringy(val: Any) -> Any:
    """Redact token-bearing strings that slip into contexts / breadcrumbs."""
    if not isinstance(val, str):
        return val
    low = val.lower()
    needles = (
        "token=", "session=", "access_token=", "bearer ", "password=",
        "so_session", "emchg_", "sol_live_", "whsec_", "sk_live", "sk_test",
    )
    if any(n in low for n in needles):
        # Keep a short prefix so ops can still tell *what kind* of value it was.
        return (val[:24] + "…[redacted]") if len(val) > 24 else "[redacted]"
    return val


def _scrub_deep(data: Any) -> Any:
    """_scrub keys + scrub string values that look like secrets."""
    data = _scrub(data)
    if isinstance(data, dict):
        return {k: _scrub_deep(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return type(data)(_scrub_deep(v) for v in data)
    return _scrub_stringy(data)


def _before_send(event: dict, hint: dict) -> dict:
    """Sentry hook: scrub headers/cookies/body/contexts/breadcrumbs before send."""
    try:
        req = event.get("request")
        if isinstance(req, dict):
            for key in ("headers", "cookies", "data", "query_string", "url"):
                if key in req and req[key] is not None:
                    req[key] = _scrub_deep(req[key])
        # Drop any captured local variables that look sensitive in stack frames.
        for ex in (event.get("exception", {}) or {}).get("values", []) or []:
            for frame in (ex.get("stacktrace", {}) or {}).get("frames", []) or []:
                if isinstance(frame.get("vars"), dict):
                    frame["vars"] = _scrub_deep(frame["vars"])
        # T2-9: contexts (e.g. client url/stack) and breadcrumbs from LoggingIntegration
        # were previously unscrubbed — token-bearing URLs could ship to Sentry.
        if isinstance(event.get("contexts"), dict):
            event["contexts"] = _scrub_deep(event["contexts"])
        if isinstance(event.get("extra"), dict):
            event["extra"] = _scrub_deep(event["extra"])
        crumbs = (event.get("breadcrumbs") or {}).get("values")
        if isinstance(crumbs, list):
            for c in crumbs:
                if not isinstance(c, dict):
                    continue
                if "message" in c:
                    c["message"] = _scrub_stringy(c.get("message"))
                if isinstance(c.get("data"), dict):
                    c["data"] = _scrub_deep(c["data"])
    except Exception:  # never let scrubbing break error reporting
        log.exception("sentry _before_send scrub failed")
    return event


def init_sentry() -> bool:
    """Initialize Sentry IFF SENTRY_DSN is set. Returns True if enabled.

    Safe to call once at startup. Silent no-op (returns False) when the DSN is
    absent or the SDK can't be imported — never raises.
    """
    global _ENABLED
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        log.info("Sentry disabled (no SENTRY_DSN)")
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except Exception:
        log.warning("SENTRY_DSN set but sentry_sdk import failed — error monitoring OFF")
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            release=os.getenv("SENTRY_RELEASE") or None,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            # We do our own scrubbing and never want request bodies by default.
            send_default_pii=False,
            # Local vars are the landmine: CredentialIn body / Creds dataclass
            # can embed password= in repr even when request PII is off.
            include_local_variables=False,
            before_send=_before_send,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
        _ENABLED = True
        log.info("Sentry initialized (env=%s)", os.getenv("SENTRY_ENVIRONMENT", "production"))
        return True
    except Exception:
        log.exception("Sentry init failed — continuing without error monitoring")
        return False


def capture_exception(exc: BaseException) -> None:
    """Forward an exception to Sentry if enabled; no-op otherwise. Never raises."""
    if not _ENABLED:
        return
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        log.exception("sentry capture_exception failed")
