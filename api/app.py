"""
NEPOOL Operator — FastAPI app.

Endpoints:
  POST /v1/sync                 — extension sends captured session here
  GET  /v1/tenants/{id}/status  — what's the state of this tenant's pipeline
  GET  /v1/tenants/{id}/bills   — every parsed bill for a tenant
  POST /v1/tenants/{id}/pull    — manually trigger a pull-bills run
  POST /v1/signup               — public signup → Stripe Checkout
  POST /v1/stripe/webhook       — Stripe → activate tenant + email code
  GET  /health                  — liveness probe (Railway)

Admin (no auth in MVP — guard with a deploy-time env in prod):
  POST /admin/tenants           — create a tenant
  POST /admin/jobs/run          — run pending jobs (also runs on a scheduler)
  GET  /admin/tenants           — list all
"""
from __future__ import annotations
import io, os, pathlib, secrets, shutil, json, logging, tarfile, hmac
from datetime import datetime
from typing import Any
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select, func, or_
from .db import init_db, SessionLocal, pool_status, PoolTimeout, record_pool_timeout
from .models import Tenant, Client, UtilityAccount, UtilitySession, Bill, Job, Array, now
from .fuels import normalize_fuel
from .adapters import get_adapter, is_smarthub_provider
from .sync_filter import classify_residential
import re as _re

_SYNC_EMAIL_RE = _re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def _find_array_to_absorb_into(db, tenant_id, owner_id, provider, captured_name):
    """Smart GMP-absorption matcher: given a captured account's name, find an
    EXISTING array of this owner that the account should ATTACH to instead of
    spawning a duplicate (the GMP↔vendor twin problem).

    Tiers, deterministic + no fuzzy guessing:
      1. EXACT normalized-name match to an owner array with no account of this
         provider yet (the original behavior).
      2. CROSS-SOURCE CONTAINMENT: one normalized name fully contains the other
         (e.g. GMP "Londonderry Community Solar" vs vendor "Londonderry"), the
         candidate has a vendor InverterConnection (so it's the vendor twin), and
         the names do NOT differ only by a sub-array token (Starlake North/South
         are real distinct meters — never absorb across those).
    Returns the Array to absorb into, or None to create fresh.
    """
    from .array_merge import _norm_name, _differs_only_by_subarray_token
    from .models import InverterConnection
    cap = (captured_name or "").strip()
    if not cap:
        return None
    cap_norm = _norm_name(cap)
    candidates = db.execute(
        select(Array).where(
            Array.tenant_id == tenant_id,
            Array.client_id == owner_id,
            Array.deleted_at.is_(None),
        ).order_by(Array.id)
    ).scalars().all()
    if not candidates:
        return None

    cand_ids = [arr.id for arr in candidates]

    # Bulk-load both membership signals in TWO grouped queries (was 2 COUNT
    # queries PER candidate inside the loops below — an N+1 on the hot /v1/sync
    # path, multiplied again by the per-account caller loop).
    _prov_acct_ids = {
        aid for (aid,) in db.execute(
            select(UtilityAccount.array_id).where(
                UtilityAccount.array_id.in_(cand_ids),
                UtilityAccount.provider == provider,
                UtilityAccount.deleted_at.is_(None),
            ).group_by(UtilityAccount.array_id)
        ).all()
    }
    _vendor_ids = {
        aid for (aid,) in db.execute(
            select(InverterConnection.array_id).where(
                InverterConnection.array_id.in_(cand_ids),
            ).group_by(InverterConnection.array_id)
        ).all()
    }

    def _no_provider_account(arr):
        return arr.id not in _prov_acct_ids

    def _has_vendor(arr):
        return arr.id in _vendor_ids

    # Tier 1: exact normalized-name match, no existing account of this provider.
    for arr in candidates:
        if _norm_name(arr.name) == cap_norm and _no_provider_account(arr):
            return arr
    # Tier 2: cross-source containment against a VENDOR array (the twin), guarded.
    for arr in candidates:
        an = _norm_name(arr.name)
        if not an or an == cap_norm:
            continue
        if not (cap_norm in an or an in cap_norm):
            continue
        if _differs_only_by_subarray_token(arr.name, cap):
            continue
        if _has_vendor(arr) and _no_provider_account(arr):
            return arr
    return None




def _smart_client_name(
    user_dict: dict,
    accounts: list[dict],
    user_email: str,
    user_username: str,
) -> str:
    """Compute the best human-readable client name from captured portal data.

    Priority:
    1. Account-level customer_name / extra.customerName (entity name; VEC exposes this
       as the company/farm the account is billed under — most reliable client label)
    2. Account holder name from the portal user profile (user.name, user.fullName, etc.)
    3. Local-part of login email, de-dotted + title-cased ("john.doe" → "John Doe")
    4. Username as-is
    5. "New client" fallback
    """
    # 1. Account-level entity name (e.g. VEC customerName)
    for a in accounts:
        extra = a.get("extra") or {}
        cname = (
            a.get("customer_name")
            or extra.get("customerName")
            or extra.get("customer_name")
        )
        if cname and str(cname).strip():
            s = str(cname).strip()
            # Scraped SmartHub names arrive ALL-CAPS ("RICHARD G EVANS") —
            # title-case for display.
            if s.isupper():
                s = s.title()
            return s[:200]

    # 2. Portal user profile name (account holder display name)
    holder = (
        (user_dict.get("name") or "").strip()
        or (user_dict.get("fullName") or "").strip()
        or (user_dict.get("display_name") or "").strip()
        or (user_dict.get("displayName") or "").strip()
    )
    if holder:
        return holder[:200]

    # 3. Local-part of email, de-dotted + title-cased. SmartHub logins use the
    #    email AS the username, so apply the same cleanup there — never return
    #    a raw email address as the client name.
    _emailish = user_email if (user_email and "@" in user_email) else (
        user_username if (user_username and "@" in user_username) else ""
    )
    if _emailish:
        local = _emailish.split("@")[0]
        cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ")
        result = cleaned.strip().title()
        return result[:200] if result else _emailish[:200]

    # 4. Username
    if user_username:
        return user_username[:200]

    return "New client"
from .worker import pull_bills_for_tenant, run_pending_jobs
# v1.1.0: api/signup.py was renamed to api/_legacy_signup.py and unmounted.
# Its Stripe webhook moved to api/stripe_webhook.py (still mounted below).
from .stripe_webhook import router as stripe_webhook_router
from .account import router as account_router
from .account import require_not_demo
from .onboarding import router as onboarding_router
from .ingest import router as ingest_router
from .billing.routes import router as billing_router
from .array_tracker import router as array_tracker_router
from .daily_generation import router as daily_generation_router
from .array_owners import router as array_owners_router
from .warranty_claims import router as warranty_claims_router
from .repair_ops import router as repair_ops_router
from .solaredge import router as solaredge_router
from .nepool_assign import router as nepool_router
from .resend_webhook import router as resend_webhook_router
from .sandbox import router as sandbox_router
from .verification import router as verification_router
from .dev_sandbox import router as dev_sandbox_router, DEV_ENABLED as _SO_DEV_ENABLED
from .dev_captures import router as dev_captures_router
from .events import router as events_router, broadcast as _sse_broadcast
from . import scheduler

log = logging.getLogger("solar_operator.app")

# Maps provider code → which Client columns drive auto-populate for that provider.
# Using string attribute names for setattr (last_sync_attr) and column descriptors
# for SQLAlchemy where-clause matching (email_col, username_col, autopop_col).
_SMARTHUB_AUTOPOP = {
    "email_col":          Client.vec_email,
    "username_col":       Client.vec_username,
    "autopop_col":        Client.vec_autopopulate,
    "last_sync_attr":     "vec_last_sync_at",
    "email_attr":         "vec_email",
    "username_attr":      "vec_username",
    "autopop_attr":       "vec_autopopulate",
    "bill_offset_months": 0,  # SmartHub bills represent the same month
}

_PROVIDER_AUTOPOP_FIELDS: dict[str, dict] = {
    "gmp": {
        "email_col":          Client.gmp_email,
        "username_col":       Client.gmp_username,
        "autopop_col":        Client.gmp_autopopulate,
        "last_sync_attr":     "gmp_last_sync_at",
        "email_attr":         "gmp_email",
        "username_attr":      "gmp_username",
        "autopop_attr":       "gmp_autopopulate",
        "bill_offset_months": 1,   # GMP bills represent the prior month
    },
}

# All SmartHub utilities share the vec_* Client columns — same portal type,
# same credential shape, no new DB columns needed.
from .adapters.smarthub import SMARTHUB_UTILITIES as _SMARTHUB_UTILITIES
for _sh_code, _sh_info in _SMARTHUB_UTILITIES.items():
    _PROVIDER_AUTOPOP_FIELDS[_sh_info["provider"]] = _SMARTHUB_AUTOPOP

# Public OpenAPI/Swagger is an attacker map of every admin + capture route.
# Keep it available in local/dev; dark it on Railway (and when explicitly set).
_ON_RAILWAY_FOR_DOCS = bool(
    os.getenv("RAILWAY_PROJECT_ID")
    or os.getenv("RAILWAY_SERVICE_ID")
    or os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("RAILWAY_ENVIRONMENT_NAME")
)
_DOCS_OFF = _ON_RAILWAY_FOR_DOCS or (os.getenv("SO_DISABLE_API_DOCS", "").lower() in ("1", "true", "yes", "on"))
app = FastAPI(
    title="NEPOOL Operator API",
    version="1.0.0",
    docs_url=None if _DOCS_OFF else "/docs",
    redoc_url=None if _DOCS_OFF else "/redoc",
    openapi_url=None if _DOCS_OFF else "/openapi.json",
)

# Error monitoring (launch readiness). Initialized as early as possible so any
# startup or request error is captured. No-op unless SENTRY_DSN is set, so dev +
# tests are unaffected. This single backend serves BOTH products (NEPOOL Operator
# and Array Operator), so this one init covers server-side errors system-wide.
from .observability import init_sentry, capture_exception  # noqa: E402
from . import observability  # noqa: E402
init_sentry()

# CORS — allow the EnergyAgent umbrella front-ends to call the shared backend.
# Two products, one API: the NEPOOL Operator marketing/app (solaroperator.org)
# and the Array Operator owner site (array-operator-ea.netlify.app, and the
# future arrayoperator.com / app.yourenergyagent.com). Any Chrome extension may
# call /v1/sync (chrome-extension:// origins) — all authenticate per-request via
# tenant_key bearer, so the origin list is a courtesy gate, not the security boundary.
ALLOWED_ORIGINS = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    ",".join([
        "https://solaroperator.org",
        "https://www.solaroperator.org",
        # EnergyAgent umbrella brand + Array Operator owner site
        "https://yourenergyagent.com",
        "https://www.yourenergyagent.com",
        "https://array-operator-ea.netlify.app",
        "https://arrayoperator.com",
        "https://www.arrayoperator.com",
        "https://app.yourenergyagent.com",
        "http://localhost:3000",
        "http://localhost:8088",  # local Array Operator static preview
    ])
).split(",")
# Allow Netlify deploy-preview / branch subdomains for both sites
# (e.g. https://<hash>--array-operator-ea.netlify.app, energyagent-250 previews).
_NETLIFY_PREVIEW_RE = r"^https://([a-z0-9-]+--)?(array-operator-ea|energyagent-250)\.netlify\.app$"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_origin_regex=r"^chrome-extension://[a-z0-9]+$|" + _NETLIFY_PREVIEW_RE,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)
# Gzip every response the browser accepts it for (Ford 2026-07-07: the 800-offtaker
# Reports tab shipped ~1MB uncompressed for /list-bundle + /drafts, and neither the
# app nor Railway's edge was compressing). JSON compresses ~7-8× → the payload
# transfer drops from ~1MB to ~120KB. Transparent (the browser decompresses);
# responses under 500 bytes skip it.
app.add_middleware(GZipMiddleware, minimum_size=500)


# ── Security headers (CSP + clickjacking / MIME / referrer hardening) ─────────
# This backend serves the React dashboard SPA (/accounts) + onboarding for BOTH
# products through the Netlify 200-proxy, so these headers protect the
# authenticated surface of nepooloperator.com AND arrayoperator.com — Netlify
# _headers can't, because it doesn't apply to proxied responses. The CSP allowlist
# is exactly what the dashboard build loads: self (Vite bundle + same-origin /v1),
# Google Fonts, data/blob images. No external scripts, no eval. 'unsafe-inline' is
# kept for the inline title script + React inline styles (nonce refactor is a
# separate pass). CSP on JSON/API responses is inert; the other headers harden them.
_DASH_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' https://web-production-49c83.up.railway.app; "
    "worker-src 'self' blob:; frame-src 'self' blob:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")
    h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # CSP everywhere EXCEPT the MC-generated preview bundles (unknown external needs).
    if not request.url.path.startswith("/accounts/preview/"):
        h.setdefault("Content-Security-Policy", _DASH_CSP)
    return resp


# ── Global exception handler (launch readiness) ──────────────────────────────
# Defense in depth so an unhandled 500 in prod is NEVER fully silent, even if
# Sentry is unconfigured or down:
#   1. forward to Sentry (no-op if disabled),
#   2. email an internal alert (throttled so an error storm can't spam us),
#   3. return a clean JSON 500 (no stack trace leaked to the client).
# HTTPException / Starlette's own HTTP errors are intentionally NOT caught here —
# those are normal control flow (401/404/etc.) and have their own handlers.
import time as _time
_LAST_ALERT: dict[str, float] = {}
_ALERT_COOLDOWN_S = 300  # at most one email per error signature per 5 min


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    # Let HTTP exceptions fall through to their normal handlers.
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        raise exc
    path = request.url.path
    # A client that hung up mid-request (Starlette ClientDisconnect) is not a
    # server fault — the socket is already gone, so returning a body is moot and
    # Sentry-capturing / paging on it is pure noise (seen: POST /v1/sync). Ack
    # quietly with 499 (client closed request) and skip the alert.
    if type(exc).__name__ == "ClientDisconnect":
        log.info("client disconnected mid-request on %s", path)
        return JSONResponse(status_code=499,
                            content={"ok": False, "error": "client disconnected"})
    log.exception("Unhandled error on %s", path)
    try:
        capture_exception(exc)
    except Exception:
        log.exception("capture_exception failed")
    # Throttled email fallback, keyed by path + exception type.
    try:
        sig = f"{path}|{type(exc).__name__}"
        now = _time.monotonic()
        last = _LAST_ALERT.get(sig, 0.0)
        if now - last >= _ALERT_COOLDOWN_S:
            _LAST_ALERT[sig] = now
            from .notify import send_internal_alert
            send_internal_alert(
                f"500 error: {type(exc).__name__} on {path}",
                f"Path: {request.method} {path}\nError: {type(exc).__name__}: {exc}",
            )
    except Exception:
        log.exception("internal alert send failed")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "Something went wrong on our end. Please try again."},
    )


# v1.1.0: replaced by onboarding.router. Webhook handler still active via the new onboarding flow.
# app.include_router(signup_router)
app.include_router(stripe_webhook_router)
app.include_router(account_router)
# New 5-screen onboarding flow.
app.include_router(onboarding_router)
# V4: AI spreadsheet ingest for arrays + NEPOOL IDs.
app.include_router(ingest_router)
app.include_router(billing_router)
app.include_router(array_tracker_router)
# Phase 2 daily generation CSV ingest + coverage.
app.include_router(daily_generation_router)

app.include_router(array_owners_router)
app.include_router(warranty_claims_router)
app.include_router(repair_ops_router)
# SolarEdge Monitoring API integration (feat/solaredge-adapter).
app.include_router(solaredge_router)
# AI-assisted NEPOOL ID assignment from spreadsheet (pure assignment, no array creation).
app.include_router(nepool_router)
# W2-6: Resend delivery webhook → per-client delivery health.
app.include_router(resend_webhook_router)
# Sandbox canvas: client graph visualization + position persistence.
app.include_router(sandbox_router)
# Verify accuracy: operator uploads their records to compare against SO workbook.
app.include_router(verification_router)
# SSE live-push: streams capture.landed events to the sandbox canvas.
app.include_router(events_router)
# Dev-only sandbox helpers. Mounted but each route guards on SO_DEV_ENABLED;
# the /status route is always reachable so the SPA can decide what to render.
app.include_router(dev_sandbox_router)
# Dev-only capture timeline (gated by SO_DEV_ENABLED in each route).
app.include_router(dev_captures_router)

# CC funnel dashboard (read-only admin metrics)
from .admin_funnel import router as funnel_router
app.include_router(funnel_router)

# CC feature-suggestion capture + admin review endpoints
from .feature_suggestions import router as feature_suggestions_router
app.include_router(feature_suggestions_router)

# CC utility-add requests (Master Account picker → agent wires the utility in)
from .utility_requests import router as utility_requests_router
app.include_router(utility_requests_router)

from .portal_access import router as portal_access_router
app.include_router(portal_access_router)

# Array Prospectus (Array Secondary Market v0): per-array verified data-room
# artifact + revocable tokenized public share link. No money, document-only.
from .prospectus_routes import router as prospectus_router
app.include_router(prospectus_router)
from .rec_desk_routes import router as rec_desk_router
app.include_router(rec_desk_router)

# Cloud Capture: server-side headless-browser harvesting (the opt-in alternative
# to the browser extension). Collects/toggles/deletes server-side portal
# credentials + reports harvest status. See api/harvester/ for the engine.
from .cloud_capture import router as cloud_capture_router
app.include_router(cloud_capture_router)

# Auto-adapter engine: self-improving declarative adapters (synth -> validate -> registry)
from .auto_adapters import router as auto_adapters_router
app.include_router(auto_adapters_router)

# Energy Agent — voice-first tenant operator (orb + tools + dual memory)
from .energy_agent import router as energy_agent_router
app.include_router(energy_agent_router)
from .energy_agent_mind import router as energy_agent_mind_router
app.include_router(energy_agent_mind_router)
# Owner ⇄ Energy Agent email channel (weekly check-in + reply-to-act + opt-out)
from .energy_agent_email import router as energy_agent_email_router
app.include_router(energy_agent_email_router)
# Sovereign Mind (product executive) + private Ford desk chat
from .energy_agent_sovereign import router as energy_agent_sovereign_router
app.include_router(energy_agent_sovereign_router)
from .energy_agent_sovereign_desk import router as energy_agent_sovereign_desk_router
app.include_router(energy_agent_sovereign_desk_router)
from .ford_escalations import router as ford_escalations_router
app.include_router(ford_escalations_router)
if _SO_DEV_ENABLED:
    import logging
    logging.getLogger("uvicorn.error").warning(
        "SO_DEV_ENABLED=1 — /v1/dev/* seed/wipe endpoints are LIVE"
    )


@app.on_event("startup")
def _startup():
    init_db()
    # Process split: web serves HTTP only when RUN_SCHEDULER=0; worker owns
    # APScheduler + Sovereign. Default RUN_SCHEDULER=1 keeps single-process
    # deploys working until ops set web→0 / worker→1.
    from .scheduler import scheduler_enabled
    if scheduler_enabled():
        scheduler.start()
    else:
        logging.getLogger("uvicorn.error").info(
            "scheduler disabled (web role) — RUN_SCHEDULER=%r",
            os.environ.get("RUN_SCHEDULER"),
        )
    # Raise the threadpool that FastAPI runs sync routes in (default 40). Sign-up
    # and most account routes are sync (blocking DB + Resend calls), so under a
    # burst they queue here; lift the ceiling so more run concurrently. They're
    # still bounded by the DB pool, so keep this near pool size + a buffer.
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = int(
            os.environ.get("THREADPOOL_TOKENS", "64"))
    except Exception:
        pass  # non-fatal — fall back to the default limiter

    # Sovereign durability: after deploy/restart, resume orphan desk turns so
    # Ford never loses a mid-flight "thinking" reply. Runs off the main thread.
    # Only when this process owns the scheduler — avoids double recovery if both
    # web and worker would otherwise race on the same orphan rows.
    if not scheduler_enabled():
        return

    def _sovereign_boot_recover() -> None:
        import logging
        import time
        log = logging.getLogger("energy_agent.sovereign.boot")
        try:
            time.sleep(4)  # let pool + tables settle
            from .energy_agent_sovereign_desk import (
                note_sovereign_boot,
                recover_orphan_desk_turns,
            )
            note_sovereign_boot()
            res = recover_orphan_desk_turns(limit=5)
            if res.get("recovered"):
                log.warning(
                    "sovereign boot recovered %s orphan desk turn(s): %s",
                    res.get("recovered"),
                    res.get("results"),
                )
            else:
                log.info("sovereign boot: no orphan desk turns (%s)", res)
        except Exception:
            log.exception("sovereign boot recover failed")

    try:
        import threading
        threading.Thread(
            target=_sovereign_boot_recover,
            name="sov-boot-recover",
            daemon=True,
        ).start()
    except Exception:
        pass


@app.get("/health")
async def health():
    """Liveness + non-secret readiness diagnostics.

    CRITICAL: this handler is **async** so it never competes for the sync
    threadpool. The 2026-07-14 outage mode was: every sync worker blocked on
    QueuePool checkout → even /health hung → Railway couldn't recover. Pool
    stats below use counter introspection only (no DB checkout).
    """
    # Additive, non-secret readiness diagnostics so we can verify at a glance that
    # prod is on Postgres (not the SQLite fallback) and that email/billing are
    # wired. Booleans only — no keys or values are exposed. Still returns ok:true
    # so the Railway healthcheck contract is unchanged.
    try:
        ps = pool_status()
        dialect = ps.get("dialect") or "unknown"
        pool_max = ps.get("capacity")
    except Exception:
        dialect, pool_max, ps = "unknown", None, {}
    return {
        "ok": True,
        "service": "solar-operator-api",
        "db": dialect,                       # "postgresql" = good; "sqlite" = NOT production-ready
        "db_pool_max": pool_max,
        # Live pool utilization — no checkout. pressure=true means near exhaustion.
        "db_pool_checked_out": ps.get("checked_out"),
        "db_pool_pressure": bool(ps.get("pressure")),
        "db_pool_timeouts": ps.get("timeouts"),
        "email_configured": bool(os.getenv("RESEND_API_KEY")),
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY")),
        "stripe_array_price_set": bool(os.getenv("STRIPE_ARRAY_PRICE_ID")),
        "sentry_configured": bool(os.getenv("SENTRY_DSN")),
        "web_concurrency": int(os.getenv("WEB_CONCURRENCY", "1")),
        # Non-secret crypto posture: True means SO_CONFIG_KEY is set so vendor /
        # utility / cloud-capture secrets encrypt at rest. False = plaintext pass-through.
        "encryption_at_rest": bool(os.getenv("SO_CONFIG_KEY")),
        "api_docs_public": not _DOCS_OFF,
    }


@app.exception_handler(PoolTimeout)
async def _pool_timeout_handler(request: Request, exc: PoolTimeout):
    """Fail-fast when the SQLAlchemy pool is exhausted.

    Was: hang every request thread for pool_timeout seconds → cascade outage.
    Now: 503 + Retry-After so clients back off and workers free immediately.
    """
    record_pool_timeout()
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Server is busy right now — please try again in a few seconds.",
            "code": "db_pool_exhausted",
        },
        headers={"Retry-After": "5"},
    )


@app.post("/v1/client-error")
async def client_error(request: Request):
    """Receive a browser/extension JS error and route it through the SAME
    server-side monitoring pipeline (Sentry capture + throttled internal alert).

    This gives whole-system error coverage with ONE config (the backend's
    SENTRY_DSN): arrayoperator.com, the NEPOOL frontend, and the Chrome extension
    all POST here instead of each needing their own Sentry project. Best-effort,
    unauthenticated (errors can happen pre-login), rate-limited per-IP, and the
    payload is capped + scrubbed so it can't be abused as a spam vector or leak.
    """
    from . import ratelimit
    # Cheap public endpoint — cap abuse. 30 reports / 5 min / IP is plenty for a
    # real user yet stops a tab stuck in an error loop from hammering us.
    if not ratelimit.allow("client_error", ratelimit.client_ip(request),
                            max_hits=30, window_s=300):
        return {"ok": True, "throttled": True}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad json"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad payload"})

    def _clip(v, n):
        return (str(v)[:n] if v is not None else "")
    source = _clip(body.get("source"), 40) or "browser"      # "arrayoperator" | "nepool" | "extension"
    message = _clip(body.get("message"), 500)
    stack = _clip(body.get("stack"), 4000)
    url = _clip(body.get("url"), 300)
    ua = _clip(request.headers.get("user-agent"), 300)
    if not message and not stack:
        return {"ok": True, "ignored": "empty"}

    log.warning("client_error[%s] %s | url=%s", source, message, url)

    # Forward to Sentry (no-op if unconfigured) with client context.
    try:
        if observability.is_enabled():
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("client_source", source)
                scope.set_context("client", {"url": url, "user_agent": ua, "stack": stack})
                sentry_sdk.capture_message(f"[{source}] {message}", level="error")
    except Exception:
        log.exception("client_error sentry forward failed")

    # Throttled internal email — reuse the same cooldown table as the 500 handler
    # so a flood of client errors can't spam the inbox.
    try:
        sig = f"client:{source}|{message[:80]}"
        now = _time.monotonic()
        if now - _LAST_ALERT.get(sig, 0.0) >= _ALERT_COOLDOWN_S:
            _LAST_ALERT[sig] = now
            from .notify import send_internal_alert
            send_internal_alert(
                f"Client error [{source}]: {message[:80]}",
                f"Source: {source}\nURL: {url}\nUA: {ua}\n\n{message}\n\n{stack}",
            )
    except Exception:
        log.exception("client_error internal alert failed")
    return {"ok": True}


@app.post("/v1/extension/heartbeat")
async def extension_heartbeat(request: Request,
                              authorization: str | None = Header(default=None)):
    """Lightweight periodic ping from the Chrome extension so the onboarding
    screen can distinguish 'extension installed and active' from 'not detected.'
    Called by background.js every 60s when on a GMP tab. Returns the server
    timestamp so the extension can show clock-skew warnings if needed.

    v1.9.112: the ping MAY carry a vault report — {"vault": [{code, username,
    enabled, last_ok_at, fails, paused}, …]} — listing which utility portal
    logins are saved in the extension's client-side vault. Usernames + health
    ONLY; passwords never leave the operator's machine by design. Persisted to
    PortalLoginStatus (per-provider replace) for the dashboard "Portal access"
    tab. Body-less pings (the normal case, 59 of 60) skip all of this."""
    tenant = tenant_from_bearer(authorization)
    require_not_demo(tenant)
    vault_report: list[dict] | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("vault"), list):
            vault_report = body["vault"]
    except Exception:
        vault_report = None   # body-less / non-JSON ping — the common case
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if t:
            t.extension_heartbeat_at = now()
        if vault_report is not None:
            from .portal_access import ingest_vault_report
            ingest_vault_report(db, tenant.id, vault_report)
        db.commit()
        # Capture debt rides the heartbeat: if this tenant's extension-captured
        # vendors/utilities have gone stale (machine was asleep, session lapsed),
        # tell WHICHEVER browser pinged us what to re-capture. Any signed-in
        # machine drains the debt — that's the single-machine-dependency fix.
        from .capture_debt import debt_for_heartbeat, cloud_capture_providers
        debt = debt_for_heartbeat(db, tenant.id)
        # Providers this tenant has activated for server-side Cloud Capture. The
        # extension caches this and suppresses its own Chrome 'reconnect' nudges for
        # them (Ford 2026-07-11) — the server refreshes those logins, so the owner
        # shouldn't also get the extension asking them to sign in. Always sent (even
        # empty) so DEACTIVATING a login propagates and clears the extension's cache.
        cloud_capture = cloud_capture_providers(db, tenant.id)
    out = {"ok": True, "at": datetime.utcnow().isoformat(), "cloud_capture": cloud_capture}
    if debt:
        out["debt"] = debt
    return out


@app.post("/v1/extension/scrape-miss")
async def extension_scrape_miss(request: Request, authorization: str | None = Header(default=None)):
    """Drift radar: the extension reports when ALL capture layers (home-page
    API, billing-history DOM, usage-explorer) came up empty on a SmartHub
    billing/usage page. That means a deployment our parsers can't read —
    exactly the signal we need to fix the parser BEFORE a customer churns.

    Records the sighting on the DiscoveredUtility row (curated hosts get a
    row minted here too — discovery rows double as drift telemetry) and
    fires a one-time internal alert per host.
    """
    tenant = tenant_from_bearer(authorization)
    require_not_demo(tenant)
    body = await request.json()
    hostname = (body.get("hostname") or "").strip().lower()
    from .adapters.smarthub import derive_provider_from_host
    derived = derive_provider_from_host(hostname)
    if not derived:
        return {"ok": False, "reason": "not a smarthub host"}

    from .models import DiscoveredUtility
    from .notify import send_internal_alert
    with SessionLocal() as db:
        disc = db.execute(
            select(DiscoveredUtility).where(DiscoveredUtility.host == hostname)
        ).scalar_one_or_none()
        if disc is None:
            disc = DiscoveredUtility(
                host=hostname,
                provider_code=derived["provider"],
                display_name=derived["name"],
            )
            db.add(disc)
        disc.last_seen_at = now()
        disc.last_capture_method = "miss"
        if body.get("extensionVersion"):
            disc.last_extension_version = str(body["extensionVersion"])[:20]
        if disc.alerted_at is None:
            ok = send_internal_alert(
                f"SmartHub scrape MISS: {hostname}",
                f"The extension visited a billing/usage page on {hostname} and "
                "ALL capture layers (API, DOM, usage-explorer) returned 0 rows.\n\n"
                f"Provider: {derived['provider']}\n"
                f"Page: {body.get('page')}\n"
                f"Extension: v{body.get('extensionVersion') or '?'}\n"
                f"Tenant: {tenant.id}\n\n"
                "This deployment likely uses a layout our parsers don't know. "
                "Capture a HAR/screenshot from this host and extend the parser.",
            )
            if ok:
                disc.alerted_at = now()
        db.commit()
    return {"ok": True}


# ---- helpers ------------------------------------------------------------

def tenant_from_bearer(authorization: str | None) -> Tenant:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    with SessionLocal() as db:
        t = db.execute(select(Tenant).where(Tenant.tenant_key == key)).scalar_one_or_none()
        if not t or not t.active:
            raise HTTPException(403, "Invalid or inactive tenant key")
        return t


# ---- ingest: receives extension POSTs ----------------------------------

@app.post("/v1/sync")
async def sync(request: Request, authorization: str | None = Header(default=None)):
    """Chrome extension sends the captured session here. We persist a new
    UtilitySession row and upsert UtilityAccount rows."""
    tenant = tenant_from_bearer(authorization)
    require_not_demo(tenant)
    payload = await request.json()
    adapter = get_adapter(payload.get("provider", "gmp"))
    normalized = adapter.parse_extension_payload(payload)

    from .capture_events import CaptureContext
    ctx = CaptureContext(tenant_id=tenant.id)
    ctx.add(
        "ingest_received",
        decision=f"{len(normalized.get('accounts', []))} {normalized.get('provider', '?')} account(s)",
        payload=payload,
    )

    with SessionLocal() as db:
        # ── Extension liveness: any authenticated /v1/sync is proof the
        # extension is alive and paired for this tenant, so refresh the
        # heartbeat here — NOT just on /v1/extension/heartbeat, which only
        # fires while a GMP tab is open. Background captures (util-live portal
        # rotation, recaptures, VEC/other portals) reach /v1/sync without any
        # GMP tab, so without this the dashboard's "Last seen" and the "hasn't
        # checked in for 48+ hours" banner rot to stale while data is actually
        # flowing. Stamp+commit up front so liveness is recorded even if the
        # payload below turns out to be unparseable — reaching us at all counts.
        _hb_tenant = db.get(Tenant, tenant.id)
        if _hb_tenant is not None:
            _hb_tenant.extension_heartbeat_at = now()
            db.commit()

        # ── Fleet-learning: record sightings of SmartHub deployments ────
        # Any *.smarthub.coop capture updates the sighting row for its host;
        # hosts not yet in the CSV catalog (discovered=True) trigger a
        # one-time internal alert so we promote them (one CSV line + regen)
        # and the NEXT operator gets a properly-named, first-class utility.
        if normalized.get("smarthub_host") and normalized.get("smarthub_discovered"):
            from .models import DiscoveredUtility
            from .notify import send_internal_alert
            host = normalized["smarthub_host"]
            disc = db.execute(
                select(DiscoveredUtility).where(DiscoveredUtility.host == host)
            ).scalar_one_or_none()
            if disc is None:
                disc = DiscoveredUtility(
                    host=host,
                    provider_code=normalized["provider"],
                    display_name=normalized.get("smarthub_display_name"),
                )
                db.add(disc)
            disc.last_seen_at = now()
            disc.capture_count = (disc.capture_count or 0) + 1
            if normalized.get("capture_method"):
                disc.last_capture_method = normalized["capture_method"]
            if normalized.get("extension_version"):
                disc.last_extension_version = normalized["extension_version"]
            if disc.alerted_at is None:
                ok = send_internal_alert(
                    f"New SmartHub utility discovered: {host}",
                    f"A capture just landed from {host} (not in the provider catalog).\n\n"
                    f"Provider code minted: {normalized['provider']}\n"
                    f"Display name: {normalized.get('smarthub_display_name')}\n"
                    f"Capture method: {normalized.get('capture_method') or 'unknown'}\n"
                    f"Extension: v{normalized.get('extension_version') or '?'}\n"
                    f"Accounts in payload: {len(normalized.get('accounts', []))}\n\n"
                    "Data is flowing under the discovered code already. To promote:\n"
                    f"  1. Add a row to api/data/providers/<STATE>.csv with smarthub_host={host}\n"
                    "  2. python scripts/gen_smarthub_registry_js.py\n"
                    "  3. python -m scripts.promote_discovered_utility " + host + "\n",
                )
                if ok:
                    disc.alerted_at = now()
            ctx.add(
                "utility_discovered",
                decision=f"unknown SmartHub host {host} → minted {normalized['provider']}",
            )
            db.commit()

        # store the session — keyed by the captured login's identity
        # (customer_number) so an operator who logs into multiple distinct
        # utility customers (one login per client) keeps EVERY login usable, not
        # just the latest. Re-capturing the same login upserts its row in place.
        # A capture with no single customer identity (none, or many distinct —
        # the GMP-operator norm) is stored under the NULL bucket and ALSO
        # upserts: one unkeyed row per (tenant, provider), refreshed in place,
        # with selection falling back to latest-per-provider (legacy behavior).
        expires_at = None
        if normalized["auth"].get("apiTokenExpires"):
            try:
                expires_at = datetime.fromisoformat(normalized["auth"]["apiTokenExpires"].replace("Z","+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        from .sessions import session_customer_number
        session_customer = session_customer_number(normalized["accounts"])
        api_token = normalized["auth"].get("apiToken", "")
        refresh_token = normalized["auth"].get("refreshToken")
        raw_payload = {"user": normalized["user"]}
        # Upsert the session for this login identity. When the capture has a
        # single shared customer_number we key on it; when it has none or many
        # distinct ones (the GMP-OPERATOR case — one operator login manages many
        # utility customers, so session_customer is None), we key on the NULL
        # bucket so a reconnect REFRESHES the one unkeyed row per (tenant,
        # provider) instead of inserting a duplicate on every capture. Without
        # this, prod accumulated 2-6 GMP session rows per operator (probe
        # 2026-06-21: all 24 GMP sessions had customer_number NULL), which made
        # "which session is authoritative" ambiguous and multiplied reauth
        # emails. NULL must be matched with IS NULL, not ==, or the lookup never
        # finds the existing unkeyed row.
        cust_predicate = (
            UtilitySession.customer_number == session_customer
            if session_customer is not None
            else UtilitySession.customer_number.is_(None)
        )
        existing_sess = db.execute(
            select(UtilitySession)
            .where(UtilitySession.tenant_id == tenant.id,
                   UtilitySession.provider == normalized["provider"],
                   cust_predicate)
            .order_by(UtilitySession.captured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing_sess is not None:
            existing_sess.api_token = api_token
            existing_sess.refresh_token = refresh_token
            existing_sess.expires_at = expires_at
            existing_sess.captured_at = now()
            existing_sess.raw_payload = raw_payload
            existing_sess.refresh_failures = 0
        else:
            sess = UtilitySession(
                tenant_id=tenant.id,
                provider=normalized["provider"],
                customer_number=session_customer,
                api_token=api_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                raw_payload=raw_payload,
            )
            db.add(sess)

        # upsert accounts
        provider = normalized["provider"]
        for a in normalized["accounts"]:
            if not a.get("account_number"):
                continue
            row = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tenant.id,
                    UtilityAccount.provider == provider,
                    UtilityAccount.account_number == a["account_number"],
                )
            ).scalar_one_or_none()
            extra = a.get("extra") or {}
            if a.get("current_bill_url"):
                extra["currentBillUrlBinary"] = a["current_bill_url"]
            if row is None:
                row = UtilityAccount(
                    tenant_id=tenant.id, provider=provider,
                    account_number=a["account_number"],
                    customer_number=a.get("customer_number"),
                    nickname=a.get("nickname"),
                    service_address=a.get("service_address"),
                    extra=extra,
                )
                db.add(row)
            else:
                # Resurrect a soft-deleted UA on re-capture. Without this,
                # a delete-then-re-sign-in flow leaves orphaned UAs marked
                # deleted, the autopop loop sees "array_id is set" (to a
                # soft-deleted Array), skips them, and the new Client gets
                # created with ZERO arrays — looking like nothing happened.
                if row.deleted_at is not None:
                    row.deleted_at = None
                    # Also clear array_id so the autopop branch below
                    # re-creates a fresh Array under the new owner Client.
                    # The old Array stays soft-deleted (will be hard-
                    # deleted by the cleanup job at 30d).
                    row.array_id = None
                row.customer_number = a.get("customer_number") or row.customer_number
                row.nickname = a.get("nickname") or row.nickname
                row.service_address = a.get("service_address") or row.service_address
                row.extra = {**(row.extra or {}), **extra}
                row.last_seen = now()
            row.is_residential = classify_residential(provider, a)

        # Flush so the accounts just upserted above have IDs and are findable
        # by the autopop query below (autoflush is off on this session).
        db.flush()

        # Identify residential accounts (GMP-only; VEC/WEC always returns empty
        # today — no per-account generation flag in those payloads yet).
        # TODO(skip-residential): when VEC/WEC payloads carry a generation flag, apply the same filter here.
        residential_acct_nos: set[str] = {
            a["account_number"]
            for a in normalized["accounts"]
            if a.get("account_number") and classify_residential(provider, a)
        }
        residential_count = len(residential_acct_nos)
        for _a in normalized["accounts"]:
            _acct_no = _a.get("account_number")
            if _acct_no and _acct_no in residential_acct_nos:
                ctx.add(
                    "account_skipped_residential",
                    decision=f"skipped {_acct_no} (residential — no solar net-metering)",
                )

        # ── auto-populate arrays from utility portal ─────────────────────
        # If this capture's login email/username matches a Client that opted
        # into autopop for this provider, append/link Arrays automatically.
        #
        # ⚠️⚠️  LOUD WARNING — MULTI-ACCOUNT-PER-ARRAY CASE  ⚠️⚠️
        # Autopop creates ONE Array per utility account. Real-world arrays that
        # SUM several sub-meters into a single logical array CANNOT be detected
        # here. Bruce's "Starlake" = 3 GMP accounts → 1 array (with
        # bill_offset_months=0). Autopop will instead create 3 SEPARATE arrays.
        # The operator MUST merge those duplicates manually via the dashboard
        # afterward — do NOT attempt to guess merges from account metadata.
        capture_result: str = "noop"
        capture_client_id: int | None = None
        capture_client_name: str | None = None

        _autopop_cfg = _PROVIDER_AUTOPOP_FIELDS.get(provider)
        if _autopop_cfg:
            captured_user = normalized.get("user") or {}
            user_email = (captured_user.get("email") or "").strip().lower()
            user_username = (captured_user.get("username") or "").strip().lower()
            if user_email or user_username:
                match_terms = []
                if user_email:
                    match_terms.append(func.lower(_autopop_cfg["email_col"]) == user_email)
                if user_username:
                    match_terms.append(func.lower(_autopop_cfg["username_col"]) == user_username)
                # Find ALL clients with a matching email/username, autopop or not.
                # We need to distinguish three cases:
                #   (1) at least one matching client has autopop=True  → normal autopop
                #   (2) match exists but autopop=False everywhere      → operator explicitly
                #       opted out for this login; respect it, do nothing
                #   (3) no match at all                                → adopt placeholder
                #       or auto-create a new Client for this login
                all_matches = db.execute(
                    select(Client)
                    .where(
                        Client.tenant_id == tenant.id,
                        Client.deleted_at.is_(None),
                        or_(*match_terms),
                    )
                    .order_by(Client.id)
                ).scalars().all()
                clients = [c for c in all_matches if getattr(c, _autopop_cfg["autopop_attr"])]
                if clients:
                    # If more than one matches (misconfiguration), the lowest-id
                    # Client owns the arrays; the rest just get their sync timestamp bumped.
                    owner = clients[0]
                    _match_field = _autopop_cfg["email_attr"] if user_email and getattr(owner, _autopop_cfg["email_attr"]) else _autopop_cfg["username_attr"]
                    ctx.add("client_matched", decision=f"matched {owner.name!r} on {_match_field}")
                    for a in normalized["accounts"]:
                        acct_no = a.get("account_number")
                        if not acct_no:
                            continue
                        acct = db.execute(
                            select(UtilityAccount).where(
                                UtilityAccount.tenant_id == tenant.id,
                                UtilityAccount.provider == provider,
                                UtilityAccount.account_number == acct_no,
                            )
                        ).scalar_one_or_none()
                        if acct is None:
                            continue
                        if acct_no in residential_acct_nos:
                            continue
                        # Defensive: if array_id points at a soft-deleted
                        # Array, treat the UA as detached and re-create
                        # the array under the current owner. (Earlier UA
                        # upsert clears array_id when it resurrects a
                        # soft-deleted UA, but historical data from before
                        # this fix may still have orphan pointers.)
                        if acct.array_id is not None:
                            existing_arr = db.get(Array, acct.array_id)
                            if existing_arr is None or existing_arr.deleted_at is not None:
                                acct.array_id = None
                            else:
                                ctx.add("array_skipped", decision=f"account {acct_no} already linked to array {acct.array_id}")
                                continue
                        arr_name = (a.get("nickname") or acct_no)[:200]
                        # PREFER linking to an EXISTING active array of this owner
                        # before creating a new one. Operators often pre-create
                        # arrays (spreadsheet import, manual) with the real name
                        # but no GMP account attached; a later GMP capture should
                        # ATTACH to that array, not spawn a duplicate. Match by the
                        # captured nickname against the owner's array names
                        # (case-insensitive), restricted to arrays that have no GMP
                        # account linked yet — a deterministic, no-guess link.
                        # Smart matcher: exact-name OR cross-source containment
                        # against a vendor twin (guarded against sub-array splits),
                        # so a GMP capture ATTACHES to the existing SolarEdge array
                        # instead of spawning a duplicate.
                        linked_existing = None
                        if a.get("nickname"):
                            linked_existing = _find_array_to_absorb_into(
                                db, tenant.id, owner.id, provider, arr_name)
                        if linked_existing is not None:
                            acct.array_id = linked_existing.id
                            ctx.add("array_linked",
                                    decision=f"linked account {acct_no} to existing array "
                                             f"{linked_existing.name!r} (id {linked_existing.id})")
                            if not acct.captured_client_name:
                                acct.captured_client_name = (owner.name or "")[:200]
                            if owner.is_placeholder:
                                owner.is_placeholder = False
                            continue
                        # A LIVE array may already own this exact name (names are
                        # unique among live rows). Link to it rather than colliding
                        # on INSERT / reviving a ghost into a duplicate.
                        live_twin = db.execute(
                            select(Array).where(
                                Array.tenant_id == tenant.id,
                                Array.name == arr_name,
                                Array.deleted_at.is_(None),
                            ).limit(1)
                        ).scalars().first()
                        if live_twin is not None:
                            acct.array_id = live_twin.id
                            ctx.add("array_linked",
                                    decision=f"linked account {acct_no} to existing array "
                                             f"{live_twin.name!r} (id {live_twin.id})")
                            if not acct.captured_client_name:
                                acct.captured_client_name = (owner.name or "")[:200]
                            if owner.is_placeholder:
                                owner.is_placeholder = False
                            continue
                        # Resurrect a soft-deleted Array with the same name
                        # (revive rather than orphan it). Safe now that no live
                        # array owns the name.
                        ghost_arr = db.execute(
                            select(Array).where(
                                Array.tenant_id == tenant.id,
                                Array.name == arr_name,
                                Array.deleted_at.is_not(None),
                            ).limit(1)
                        ).scalars().first()
                        if ghost_arr is not None:
                            ghost_arr.deleted_at = None
                            ghost_arr.client_id = owner.id
                            ghost_arr.bill_offset_months = _autopop_cfg["bill_offset_months"]
                            arr = ghost_arr
                        else:
                            arr = Array(
                                tenant_id=tenant.id,
                                client_id=owner.id,
                                name=arr_name,
                                bill_offset_months=_autopop_cfg["bill_offset_months"],
                                # Inherit the fuel the operator picked for this
                                # client during onboarding (defaults to solar).
                                fuel_type=normalize_fuel(
                                    getattr(owner, "default_fuel_type", None)),
                            )
                            db.add(arr)
                        db.flush()  # assign arr.id before linking
                        acct.array_id = arr.id
                        ctx.add("array_created", decision=f"created array {arr_name!r} for account {acct_no}")
                        # Record the autopop name so re-captures can detect
                        # whether the operator has since manually edited it.
                        if not acct.captured_client_name:
                            acct.captured_client_name = (owner.name or "")[:200]
                        # Real arrays landed → this client is no longer a placeholder.
                        if owner.is_placeholder:
                            owner.is_placeholder = False
                    for c in clients:
                        setattr(c, _autopop_cfg["last_sync_attr"], now())
                    # Cloud-Capture eager client (created from the stored login
                    # before any bill landed) just got its first real capture.
                    # Clear the 'Pulling bills…' flag and upgrade the login-
                    # derived placeholder name to the real portal holder name —
                    # unless the operator has already curated it (name_edited_at).
                    if getattr(owner, "capture_pending", False):
                        owner.capture_pending = False
                        if owner.name_edited_at is None:
                            better = _smart_client_name(
                                captured_user, normalized["accounts"],
                                user_email, user_username,
                            )
                            better = (better or "")[:200]
                            # Only rename if it's a real upgrade AND the name is
                            # free — uq_client_per_tenant would 500 the whole
                            # capture on a collision. On collision, keep the
                            # login-derived name (operator can merge/rename).
                            if better and better != "New client" and better != owner.name:
                                name_taken = db.execute(
                                    select(Client.id).where(
                                        Client.tenant_id == tenant.id,
                                        Client.name == better,
                                        Client.id != owner.id,
                                    ).limit(1)
                                ).scalar_one_or_none()
                                if name_taken is None:
                                    owner.name = better
                    capture_result = "updated"
                    capture_client_id = owner.id
                    capture_client_name = owner.name
                elif all_matches:
                    # Case (2): a Client matches this email/username but the
                    # operator explicitly set autopop=False on every match.
                    # Respect the choice — do NOT create new Clients or
                    # Arrays behind their back. Bump last_sync so they can
                    # see the extension reached us even though nothing was
                    # auto-imported.
                    ctx.add(
                        "client_merged",
                        decision=f"matched {all_matches[0].name!r} but autopop=False — skipped array creation",
                    )
                    for c in all_matches:
                        setattr(c, _autopop_cfg["last_sync_attr"], now())
                    capture_result = "updated"
                    capture_client_id = all_matches[0].id
                    capture_client_name = all_matches[0].name
                else:
                    # ── Placeholder adoption OR new-client auto-create ─────────
                    # No Client matched on (email|username) for this provider.
                    # Two sub-paths:
                    #   (a) tenant has a placeholder Client (seeded at signup
                    #       when no real clients pre-entered) → ADOPT it: rename,
                    #       backfill, attach arrays, clear is_placeholder.
                    #   (b) no placeholder → AUTO-CREATE a brand-new Client for
                    #       this login and attach arrays. This is the multi-
                    #       account path: 50 logins → 50 Clients, no manual
                    #       client entry ever required.
                    # Both share the same "what to name it / how to wire it"
                    # logic; the only difference is whether we insert a new row.
                    placeholder = db.execute(
                        select(Client).where(
                            Client.tenant_id == tenant.id,
                            Client.is_placeholder.is_(True),
                            Client.deleted_at.is_(None),
                        )
                        .order_by(Client.id)
                        .limit(1)
                    ).scalar_one_or_none()

                    if (user_email or user_username) and any(
                        a.get("account_number") and a["account_number"] not in residential_acct_nos
                        for a in normalized["accounts"]
                    ):
                        # ── Pick the display name ────────────────────────────
                        # Use smart name: holder name from portal profile >
                        # customer_name from account metadata > local-part of
                        # login email (de-dotted) > username. Never use raw
                        # email as the client name.
                        display_name = _smart_client_name(
                            captured_user, normalized["accounts"],
                            user_email, user_username,
                        )

                        # ── (a) Placeholder adoption ─────────────────────────
                        if placeholder is not None:
                            target = placeholder
                            # Respect operator edits: if name_edited_at is set,
                            # the operator curated this name — leave it alone.
                            if display_name and target.name_edited_at is None:
                                target.name = display_name
                        # ── (b) New-client auto-create ───────────────────────
                        else:
                            # Before INSERT, check for a soft-deleted Client
                            # with the same name on this tenant. The
                            # uq_client_per_tenant unique constraint does NOT
                            # exclude soft-deleted rows, so a delete-then-
                            # re-sign-in cycle would throw IntegrityError
                            # when the new auto-create tries to use the same
                            # display_name. Resurrect the soft-deleted row
                            # instead — preserves history, no duplicates.
                            # Also (Jun 6'26): if an ACTIVE client with the
                            # exact same name already exists (re-onboarded
                            # tenant manually re-creating the same name),
                            # adopt it rather than 500-ing on UNIQUE.
                            chosen_name = (display_name or "New client")[:200]
                            existing_active = db.execute(
                                select(Client).where(
                                    Client.tenant_id == tenant.id,
                                    Client.name == chosen_name,
                                    Client.deleted_at.is_(None),
                                ).limit(1)
                            ).scalar_one_or_none()
                            ghost = None
                            if existing_active is None:
                                ghost = db.execute(
                                    select(Client).where(
                                        Client.tenant_id == tenant.id,
                                        Client.name == chosen_name,
                                        Client.deleted_at.is_not(None),
                                    ).limit(1)
                                ).scalar_one_or_none()
                            if existing_active is not None:
                                target = existing_active
                            elif ghost is not None:
                                ghost.deleted_at = None
                                ghost.active = True
                                target = ghost
                            else:
                                target = Client(
                                    tenant_id=tenant.id,
                                    name=chosen_name,
                                    active=True,
                                )
                                db.add(target)
                                db.flush()

                        # Backfill the login fields + autopop flag so the NEXT
                        # capture from the same login goes through the normal
                        # autopop branch above.
                        if user_email and not getattr(target, _autopop_cfg["email_attr"]):
                            setattr(target, _autopop_cfg["email_attr"], user_email)
                        if user_username and not getattr(target, _autopop_cfg["username_attr"]):
                            setattr(target, _autopop_cfg["username_attr"], user_username)
                        setattr(target, _autopop_cfg["autopop_attr"], True)
                        setattr(target, _autopop_cfg["last_sync_attr"], now())

                        # Auto-populate contact_email from the captured login
                        # email — the address the customer uses to log into
                        # their utility portal is almost certainly the address
                        # they want reports sent to. Operator can override
                        # later from the client card. We only set this when:
                        #   (a) we have a real email (not a username), and
                        #   (b) contact_email isn't already set (don't stomp
                        #       an operator-curated value)
                        if user_email and not target.contact_email:
                            target.contact_email = user_email

                        _adopted = placeholder is not None
                        ctx.add(
                            "client_created",
                            decision=f"{'adopted placeholder' if _adopted else 'created'} client {(display_name or 'New client')!r}",
                        )

                        # Attach the captured arrays.
                        for a in normalized["accounts"]:
                            acct_no = a.get("account_number")
                            if not acct_no:
                                continue
                            acct = db.execute(
                                select(UtilityAccount).where(
                                    UtilityAccount.tenant_id == tenant.id,
                                    UtilityAccount.provider == provider,
                                    UtilityAccount.account_number == acct_no,
                                )
                            ).scalar_one_or_none()
                            if acct is None:
                                continue
                            if acct_no in residential_acct_nos:
                                continue
                            # Same defensive check as the autopop branch:
                            # array_id may point at a soft-deleted Array
                            # from a prior captured-then-deleted cycle.
                            if acct.array_id is not None:
                                existing_arr = db.get(Array, acct.array_id)
                                if existing_arr is None or existing_arr.deleted_at is not None:
                                    acct.array_id = None
                                else:
                                    ctx.add("array_skipped", decision=f"account {acct_no} already linked to array {acct.array_id}")
                                    continue
                            arr_name = (a.get("nickname") or acct_no)[:200]
                            # Smart absorb: attach to an existing vendor twin
                            # instead of creating a duplicate (same matcher as the
                            # autopop branch).
                            linked_existing = None
                            if a.get("nickname"):
                                linked_existing = _find_array_to_absorb_into(
                                    db, tenant.id, target.id, provider, arr_name)
                            if linked_existing is not None:
                                acct.array_id = linked_existing.id
                                ctx.add("array_linked",
                                        decision=f"linked account {acct_no} to existing array "
                                                 f"{linked_existing.name!r} (id {linked_existing.id})")
                                if not acct.captured_client_name:
                                    acct.captured_client_name = (target.name or "")[:200]
                                continue
                            # A LIVE array may already own this exact name (names
                            # are unique among live rows). Link to it rather than
                            # colliding on INSERT / reviving a ghost into a dup.
                            live_twin = db.execute(
                                select(Array).where(
                                    Array.tenant_id == tenant.id,
                                    Array.name == arr_name,
                                    Array.deleted_at.is_(None),
                                ).limit(1)
                            ).scalars().first()
                            if live_twin is not None:
                                acct.array_id = live_twin.id
                                ctx.add("array_linked",
                                        decision=f"linked account {acct_no} to existing array "
                                                 f"{live_twin.name!r} (id {live_twin.id})")
                                if not acct.captured_client_name:
                                    acct.captured_client_name = (target.name or "")[:200]
                                continue
                            ghost_arr = db.execute(
                                select(Array).where(
                                    Array.tenant_id == tenant.id,
                                    Array.name == arr_name,
                                    Array.deleted_at.is_not(None),
                                ).limit(1)
                            ).scalars().first()
                            if ghost_arr is not None:
                                ghost_arr.deleted_at = None
                                ghost_arr.client_id = target.id
                                ghost_arr.bill_offset_months = _autopop_cfg["bill_offset_months"]
                                arr = ghost_arr
                            else:
                                arr = Array(
                                    tenant_id=tenant.id,
                                    client_id=target.id,
                                    name=arr_name,
                                    bill_offset_months=_autopop_cfg["bill_offset_months"],
                                    # Inherit the fuel the operator picked for
                                    # this client during onboarding (def. solar).
                                    fuel_type=normalize_fuel(
                                        getattr(target, "default_fuel_type", None)),
                                )
                                db.add(arr)
                            db.flush()
                            acct.array_id = arr.id
                            ctx.add("array_created", decision=f"created array {arr_name!r} for account {acct_no}")
                            # Record the autopop name so re-captures can detect
                            # whether the operator has since manually edited it.
                            if not acct.captured_client_name:
                                acct.captured_client_name = (target.name or "")[:200]
                        if target.is_placeholder:
                            target.is_placeholder = False
                        db.flush()  # ensure target.id is populated before we read it
                        capture_result = "created"
                        capture_client_id = target.id
                        capture_client_name = target.name

        # ── SmartHub bill/usage persistence (extension-scraped) ─────────
        # GMP has a separate worker that pulls bill PDFs server-side.
        # SmartHub portals use cookie-based auth for the DOM scrape, so the
        # extension is the primary path for billing history; it POSTs
        # bills_raw + usage_raw in the payload and we persist them here.
        # Covers VEC, WEC, STOWE, and every other SmartHub utility.
        if is_smarthub_provider(provider):
            from .adapters.smarthub import parse_bill as _vec_parse_bill, parse_usage as _vec_parse_usage
            # Build an account-number → UtilityAccount.id map from this tenant's accounts
            # NOTE: must match THIS capture's provider — was hardcoded "vec",
            # which silently dropped every WEC/Stowe/other-SmartHub bill.
            acct_map = {
                r.account_number: r.id
                for r in db.execute(
                    select(UtilityAccount).where(
                        UtilityAccount.tenant_id == tenant.id,
                        UtilityAccount.provider == provider,
                    )
                ).scalars().all()
            }
            # Usage rows keyed by (account_number, period_end) so we can attach kWh
            usage_by_acct: dict[str, dict[str, dict]] = {}
            for u_raw in (normalized.get("usage_raw") or []):
                u = _vec_parse_usage(u_raw.get("aria_label", "") if isinstance(u_raw, dict) else str(u_raw))
                if not u:
                    continue
                acct_no = (u_raw.get("account_id") if isinstance(u_raw, dict) else None)
                # When the aria-label scrape doesn't carry an account_id, fall back to
                # the only account in this payload (single-account VEC logins).
                if not acct_no and len(acct_map) == 1:
                    acct_no = next(iter(acct_map.keys()))
                if not acct_no or not u["period_end"]:
                    continue
                usage_by_acct.setdefault(acct_no, {})[u["period_end"].strftime("%Y-%m")] = u

            # Accounts touched by a bill this sync → after the loop we append each
            # offtaker's generation-spreadsheet row (SmartHub utilities never hit the
            # GMP server pull that normally does this).
            _tracker_accts: set[int] = set()
            for b_raw in (normalized.get("bills_raw") or []):
                b = _vec_parse_bill(b_raw)
                acct_no = b.get("account_id")
                acct_id = acct_map.get(acct_no)
                if not acct_id or not b.get("billing_date"):
                    continue
                # Skip a SHELL bill — no meter-read period AND no stable uuid. The
                # server-side harvester emits these when a NISC overview row comes back
                # lean; they carry no billable data and, with a recent bill_date, would
                # only shadow the real (extension-captured) bills. Never persist one
                # (Ford 2026-07-11 — the Town of Glover "no bill on file" junk).
                if not b.get("period_end") and not b.get("bill_uuid"):
                    continue
                _tracker_accts.add(acct_id)
                # AUTOMATIC VEC bill-PDF pull: the extension attaches the bill PDF
                # (base64) per bill. The generation sent-to-grid + the bill's OWN
                # net-meter credit rate live ONLY on the PDF (the SmartHub APIs
                # return totalUsage:0 for a net-meter credit account). Parse it →
                # an AUTHORITATIVE settled net-meter Bill (the GMP offtaker-credit
                # path then auto-prices the invoice from the bill's own rate).
                _nm = None
                _pdf_raw = None   # decoded PDF bytes → persisted to Bill.pdf_bytes
                _pdf_b64 = b_raw.get("pdf_b64") if isinstance(b_raw, dict) else None
                if _pdf_b64:
                    try:
                        import base64 as _b64
                        from .adapters.vec_bill import parse_vec_bill_pdf as _parse_vec_pdf
                        _pdf_raw = _b64.b64decode(_pdf_b64)
                        _nm = _parse_vec_pdf(_pdf_raw)
                    except Exception:
                        _pdf_raw = None
                        _nm = None
                doc_no = b.get("bill_uuid") or b.get("pdf_url") or b["billing_date"].strftime("VEC-%Y-%m-%d")
                # Attach usage from the same billing month, if available.
                # API-captured bills (v1.6.0) carry kWh + meter-read period
                # inline — prefer those; usage-explorer rows remain the
                # fallback for DOM-scraped captures.
                u = usage_by_acct.get(acct_no, {}).get(b["billing_date"].strftime("%Y-%m"))
                _ps = b.get("period_start") or (u["period_start"] if u else None)
                _pe = b.get("period_end") or (u["period_end"] if u else None)
                # A parsed bill PDF is authoritative for the meter period — prefer it.
                if _nm is not None:
                    from datetime import time as _dtime
                    if _nm.get("period_start") is not None:
                        _ps = datetime.combine(_nm["period_start"], _dtime.min)
                    if _nm.get("period_end") is not None:
                        _pe = datetime.combine(_nm["period_end"], _dtime.min)
                # CRITICAL — SmartHub bill kWh is CONSUMPTION, not generation.
                # The extension sources this from billing/overview `totalUsage`
                # (and the usage-explorer aria-label kWh), which is the meter's
                # NET CONSUMPTION for the period. For a net-metering SOLAR account
                # that net-exports, totalUsage is ~0 — so writing it into
                # kwh_generated (which the GMCS report reads as production) made
                # every VEC/WEC NEPOOL report render zeros (live: acct 6578300 had
                # 36 bills all kwh_generated=0). SmartHub bills carry NO generation
                # number; the ONLY generation source is the daily utility-usage
                # pull (negative net-export → DailyGeneration, source=utility_meter
                # / smarthub). So route this to kwh_consumed and NEVER touch
                # kwh_generated from the bill path.
                _consumed = b.get("kwh")
                if _consumed is None and u:
                    _consumed = u["kwh"]
                exists = db.execute(
                    select(Bill).where(
                        Bill.account_id == acct_id,
                        Bill.document_number == doc_no,
                    )
                ).scalar_one_or_none()
                if exists:
                    # UPDATE-don't-skip: a later capture may finally carry the
                    # consumption kWh (e.g. an API capture replaced a DOM scrape).
                    if _consumed is not None:
                        new_consumed = int(_consumed)
                        if (exists.kwh_consumed or 0) != new_consumed:
                            exists.kwh_consumed = new_consumed
                            exists.parse_status = "parsed"
                        # Backfill period bounds if they were missing.
                        if _ps and not exists.period_start:
                            exists.period_start = _ps
                        if _pe and not exists.period_end:
                            exists.period_end = _pe
                    # AUTOMATIC VEC bill-PDF: the parsed net-meter bill is
                    # authoritative for generation + the bill's own credit. Apply it
                    # independently of consumption (a net-meter credit account reads
                    # ~0 consumption). Climb-only — only raise / set when None.
                    if _nm is not None:
                        _sent = _nm.get("kwh_sent_to_grid")
                        if _sent is not None and (exists.kwh_sent_to_grid is None
                                                  or float(_sent) > exists.kwh_sent_to_grid):
                            exists.kwh_sent_to_grid = float(_sent)
                        _gen = _nm.get("kwh_generated")
                        if _gen is not None:
                            _geni = int(round(float(_gen)))
                            if exists.kwh_generated is None or _geni > exists.kwh_generated:
                                exists.kwh_generated = _geni
                        _cred = _nm.get("solar_credit_usd")
                        if _cred is not None and (exists.solar_credit_usd is None
                                                  or float(_cred) > exists.solar_credit_usd):
                            exists.solar_credit_usd = float(_cred)
                        exists.is_net_metered = True
                        exists.parse_status = "parsed"
                        if _ps and not exists.period_start:
                            exists.period_start = _ps
                        if _pe and not exists.period_end:
                            exists.period_end = _pe
                    # Persist the verbatim bill PDF bytes so the provider-aware
                    # auto-attach path (delivery.generate_files) can ride this bill
                    # onto the offtaker's invoice. Always keep the latest capture.
                    if _pdf_raw:
                        exists.pdf_bytes = _pdf_raw
                        exists.pdf_content_type = "application/pdf"
                    continue
                db.add(Bill(
                    tenant_id=tenant.id,
                    account_id=acct_id,
                    bill_date=b["billing_date"],
                    period_start=_ps,
                    period_end=_pe,
                    # generation is unknown from a SmartHub bill's consumption number —
                    # leave kwh_generated None so the report's bill-prorate path never
                    # treats consumption as production; DailyGeneration (daily pull) is
                    # the truth source. BUT when a parsed bill PDF is attached (_nm),
                    # IT is authoritative for the net-meter generation + the bill's own
                    # credit — set those so the GMP offtaker-credit path auto-prices.
                    kwh_generated=(int(round(float(_nm["kwh_generated"])))
                                   if (_nm is not None and _nm.get("kwh_generated") is not None)
                                   else None),
                    kwh_sent_to_grid=(float(_nm["kwh_sent_to_grid"])
                                      if (_nm is not None and _nm.get("kwh_sent_to_grid") is not None)
                                      else None),
                    solar_credit_usd=(float(_nm["solar_credit_usd"])
                                      if (_nm is not None and _nm.get("solar_credit_usd") is not None)
                                      else None),
                    is_net_metered=(True if _nm is not None else None),
                    kwh_consumed=int(_consumed) if _consumed is not None else None,
                    document_number=doc_no,
                    pdf_path=b.get("pdf_url"),
                    # Verbatim bill PDF bytes → durable source for provider-aware
                    # auto-attach (rides the offtaker invoice). None when the
                    # extension didn't send a PDF for this bill.
                    pdf_bytes=_pdf_raw,
                    pdf_content_type=("application/pdf" if _pdf_raw else None),
                    parse_status=("parsed"
                                  if (_nm is not None or _consumed is not None)
                                  else "partial"),
                ))

            # SmartHub utilities (VEC/WEC/…) land bills via the extension, not the
            # GMP server pull — so append each touched offtaker's generation-spreadsheet
            # row here too (the GMP pull does this in worker._tracker_append_for_account).
            # Gated + idempotent + best-effort; the db.commit() below persists it.
            if _tracker_accts:
                from .billing import sheet_tracker as _sheet_tracker
                for _aid in _tracker_accts:
                    _sheet_tracker.maybe_append_for_account(db, tenant.id, _aid)
                # Bills landed this sync → surface their generation in the daily
                # stream NOW (idempotent bill_prorate fill; real metered days
                # win) instead of at the 05:30 cron — a bills-only tenant must
                # not read all-zeros for a day after connecting. Best-effort,
                # savepoint-guarded so it can never poison the sync tx.
                try:
                    from .jobs.bill_to_daily import transform_tenant_bills
                    with db.begin_nested():
                        transform_tenant_bills(tenant.id, db=db)
                except Exception:
                    log.warning("bill→daily transform after sync failed for %s",
                                tenant.id, exc_info=True)

        # CaptureEvent rows are a best-effort audit trail. Flush them inside a
        # SAVEPOINT so a bad event can't poison — or partially commit into — the
        # main sync transaction (accounts/arrays/sessions already added above).
        # On failure the nested block rolls back ONLY the events, the sync still
        # commits cleanly, and we drop the unflushed events so they aren't
        # silently retried on a later commit.
        try:
            with db.begin_nested():
                ctx.flush(db)
        except Exception:
            log.warning("CaptureEvent flush failed for tenant %s", tenant.id, exc_info=True)
            ctx.discard()
        db.commit()

    # DATA SPONGE: the moment they finish logging into GMP, absorb their ENTIRE
    # energy history (3+ years) into their account — with live progress so the UI
    # shows an "importing your N years…" bar. For GMP we run the sponge (full
    # energy record + progress); other providers keep the plain bill pull. The 6h
    # scheduler tick is the long-tail safety net. Best-effort, never blocks the
    # response.
    try:
        from threading import Thread
        _provider = normalized.get("provider")
        _tid = tenant.id
        def _bg_absorb(tid: str, provider: str | None):
            try:
                if provider == "gmp":
                    from .sponge import absorb_history
                    absorb_history(tid, "gmp")
                else:
                    from .worker import pull_bills_for_tenant
                    pull_bills_for_tenant(tid)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "post-/v1/sync history absorb failed for %s", tid)
        Thread(target=_bg_absorb, args=(_tid, _provider), daemon=True).start()
    except Exception:
        # Never let pull-kickoff failures break the sync response.
        import logging
        logging.getLogger(__name__).exception("failed to kick off post-sync absorb")

    # Push a live event to any open SSE connections for this tenant so the
    # sandbox canvas can materialize the new/updated client immediately.
    if capture_result in ("created", "updated") and capture_client_id is not None:
        try:
            _sse_broadcast(tenant.id, "capture.landed", {
                "client_id": capture_client_id,
                "client_name": capture_client_name,
                "is_new_client": capture_result == "created",
                "residential_count": residential_count,
            })
        except Exception:
            log.exception("SSE broadcast failed for tenant %s", tenant.id)

    # FAN-OUT (feature-flagged, default OFF): if this tenant is cross-product
    # LINKED, mirror the captured utility SESSION + ACCOUNTS into the sibling and
    # kick its own bill/history absorb — the same durable capture the primary got,
    # so ONE GMP login feeds both the NEPOOL and AO tenants. The sibling's bills +
    # generation are produced by ITS absorb run from the mirrored session (the
    # same mechanism the primary uses), never fabricated. We mirror the durable
    # capture only — the primary-tenant autopop Client creation + SSE are UI
    # side-effects of the capturing tenant, not data the sibling needs. Sibling
    # failure never breaks this response (primary already committed).
    from .capture_fanout import fanout
    fanout(tenant, lambda sib: _sync_replay_into_sibling(sib, normalized))

    return {
        "ok": True,
        "tenant": tenant.id,
        "accounts": len(normalized["accounts"]),
        "residential_count": residential_count,
        "token_expires_at": normalized["auth"].get("apiTokenExpires"),
        "result": capture_result,
        "is_new_client": capture_result == "created",
        "client": {"id": capture_client_id, "name": capture_client_name} if capture_client_id else None,
    }


def _sync_replay_into_sibling(tenant: Tenant, normalized: dict) -> None:
    """Mirror a /v1/sync capture's durable state — the utility SESSION and
    ACCOUNTS — into a linked sibling tenant, then kick the same best-effort
    history/bill absorb. Used by the cross-product fan-out so one GMP login feeds
    both products. Uses the SAME upsert keys as the primary sync path
    (customer_number bucket for sessions, (tenant,provider,account_number) for
    accounts) so a re-capture refreshes in place and never duplicates."""
    from .sessions import session_customer_number
    provider = normalized["provider"]
    expires_at = None
    if normalized["auth"].get("apiTokenExpires"):
        try:
            expires_at = datetime.fromisoformat(
                normalized["auth"]["apiTokenExpires"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            pass
    api_token = normalized["auth"].get("apiToken", "")
    refresh_token = normalized["auth"].get("refreshToken")
    raw_payload = {"user": normalized["user"]}
    session_customer = session_customer_number(normalized["accounts"])

    with SessionLocal() as db:
        # Upsert the session under the same customer_number bucket as the primary.
        cust_predicate = (
            UtilitySession.customer_number == session_customer
            if session_customer is not None
            else UtilitySession.customer_number.is_(None)
        )
        existing_sess = db.execute(
            select(UtilitySession)
            .where(UtilitySession.tenant_id == tenant.id,
                   UtilitySession.provider == provider,
                   cust_predicate)
            .order_by(UtilitySession.captured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing_sess is not None:
            existing_sess.api_token = api_token
            existing_sess.refresh_token = refresh_token
            existing_sess.expires_at = expires_at
            existing_sess.captured_at = now()
            existing_sess.raw_payload = raw_payload
            existing_sess.refresh_failures = 0
        else:
            db.add(UtilitySession(
                tenant_id=tenant.id, provider=provider,
                customer_number=session_customer,
                api_token=api_token, refresh_token=refresh_token,
                expires_at=expires_at, raw_payload=raw_payload,
            ))

        # Upsert accounts (same key as the primary path).
        for a in normalized["accounts"]:
            if not a.get("account_number"):
                continue
            row = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tenant.id,
                    UtilityAccount.provider == provider,
                    UtilityAccount.account_number == a["account_number"],
                )
            ).scalar_one_or_none()
            extra = a.get("extra") or {}
            if a.get("current_bill_url"):
                extra["currentBillUrlBinary"] = a["current_bill_url"]
            if row is None:
                row = UtilityAccount(
                    tenant_id=tenant.id, provider=provider,
                    account_number=a["account_number"],
                    customer_number=a.get("customer_number"),
                    nickname=a.get("nickname"),
                    service_address=a.get("service_address"),
                    extra=extra,
                )
                db.add(row)
            else:
                if row.deleted_at is not None:
                    row.deleted_at = None
                    row.array_id = None
                row.customer_number = a.get("customer_number") or row.customer_number
                row.nickname = a.get("nickname") or row.nickname
                row.service_address = a.get("service_address") or row.service_address
                row.extra = {**(row.extra or {}), **extra}
                row.last_seen = now()
            row.is_residential = classify_residential(provider, a)
        db.commit()

    # Kick the sibling's own absorb so its bills/generation materialize from the
    # mirrored session — same mechanism the primary capture uses. Best-effort.
    try:
        from threading import Thread
        _tid = tenant.id
        def _bg(tid: str, prov: str | None):
            try:
                if prov == "gmp":
                    from .sponge import absorb_history
                    absorb_history(tid, "gmp")
                else:
                    from .worker import pull_bills_for_tenant
                    pull_bills_for_tenant(tid)
            except Exception:
                logging.getLogger(__name__).exception(
                    "sibling-sync absorb failed for %s", tid)
        Thread(target=_bg, args=(_tid, provider), daemon=True).start()
    except Exception:
        logging.getLogger(__name__).exception("failed to kick sibling-sync absorb")


# ---- tenant-facing ------------------------------------------------------

@app.get("/v1/tenants/{tid}/status")
def tenant_status(tid: str, authorization: str | None = Header(default=None)):
    tenant = tenant_from_bearer(authorization)
    if tenant.id != tid:
        raise HTTPException(403, "tenant mismatch")
    with SessionLocal() as db:
        accounts = db.execute(select(UtilityAccount).where(UtilityAccount.tenant_id == tid)).scalars().all()
        sess = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == tid).order_by(UtilitySession.captured_at.desc())
        ).scalars().first()
        bills_count = db.execute(
            select(func.count()).select_from(Bill).where(Bill.tenant_id == tid)
        ).scalar() or 0
        return {
            "tenant": tid,
            "name": tenant.name,
            "plan": tenant.plan,
            "accounts": [
                {"id": a.id, "provider": a.provider, "account_number": a.account_number,
                 "nickname": a.nickname, "enabled": a.enabled}
                for a in accounts
            ],
            "session": {
                "provider": sess.provider if sess else None,
                "captured_at": sess.captured_at.isoformat() if sess else None,
                "expires_at": sess.expires_at.isoformat() if sess and sess.expires_at else None,
            } if sess else None,
            "bills_count": int(bills_count),
        }


@app.get("/v1/tenants/{tid}/bills")
def tenant_bills(tid: str, authorization: str | None = Header(default=None)):
    tenant = tenant_from_bearer(authorization)
    if tenant.id != tid:
        raise HTTPException(403, "tenant mismatch")
    with SessionLocal() as db:
        bills = db.execute(
            select(Bill).where(Bill.tenant_id == tid).order_by(Bill.period_end.desc().nullslast())
        ).scalars().all()
        return [
            {
                "id": b.id, "account_id": b.account_id,
                "bill_date": b.bill_date.isoformat() if b.bill_date else None,
                "period_start": b.period_start.isoformat() if b.period_start else None,
                "period_end": b.period_end.isoformat() if b.period_end else None,
                "billing_days": b.billing_days,
                "kwh_generated": b.kwh_generated,
                "parse_status": b.parse_status,
                "pulled_at": b.pulled_at.isoformat(),
            } for b in bills
        ]


@app.post("/v1/tenants/{tid}/pull")
def tenant_pull(tid: str, authorization: str | None = Header(default=None)):
    tenant = tenant_from_bearer(authorization)
    require_not_demo(tenant)
    if tenant.id != tid:
        raise HTTPException(403, "tenant mismatch")
    result = pull_bills_for_tenant(tid)
    return result


# ---- admin -------------------------------------------------------------

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
# Are we running on Railway (i.e. in production)? Any of these are injected by
# the platform at runtime; none exist on a local dev box.
_ON_RAILWAY = bool(
    os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID")
    or os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT_NAME")
)


def _require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Guard admin endpoints with ADMIN_API_KEY.

    Fail CLOSED in production. Previously, if ADMIN_API_KEY was unset the guard
    fell OPEN — which left every /admin/* route (including tenant_key listing)
    reachable unauthenticated in prod. Now: if we're on Railway and no key is
    configured, deny everything (503). Locally (no Railway env) an unset key
    still allows requests so dev tooling/tests keep working.
    """
    if not ADMIN_API_KEY:
        if _ON_RAILWAY:
            raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
        return  # local dev only — open
    if not hmac.compare_digest(x_admin_key or "", ADMIN_API_KEY):
        raise HTTPException(403, "Invalid or missing X-Admin-Key")


@app.get("/admin/verify-smarthub")
def admin_verify_smarthub(
    days: int = 120,
    account: str | None = None,
    tenant: str | None = None,
    _: None = Depends(_require_admin),
):
    """Diagnostic: pull the raw SmartHub net-metering channel breakdown
    (FORWARD/RETURN/NET kWh) for captured account(s), so we can confirm which
    channel is real solar generation — the one unverified assumption gating the
    ~471 SmartHub utilities. Admin-gated; never returns auth tokens. Same logic
    as scripts/verify_smarthub_generation.py on the CLI."""
    from scripts.verify_smarthub_generation import run_verification
    return {"results": run_verification(account=account, tenant=tenant, days=days)}


@app.post("/admin/tenants")
def admin_create_tenant(body: dict, _: None = Depends(_require_admin)):
    name = body.get("name")
    email = body.get("contact_email", "")
    if not name:
        raise HTTPException(400, "name required")
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_live_" + secrets.token_urlsafe(24)
    with SessionLocal() as db:
        t = Tenant(id=tid, name=name, contact_email=email, tenant_key=key, plan="trial")
        db.add(t); db.commit()
    return {"tenant_id": tid, "tenant_key": key, "name": name}


@app.post("/admin/seed-realistic-demo")
def admin_seed_realistic_demo(arrays: int = 12, offtakers_per_array: int = 5,
                              x_seed_token: str | None = Header(default=None)):
    """Provision the realistic demo tenant — Ford's testable 'real data' account
    (large Array Operator tenant with real-shaped GMP bills so invoices, the
    bill-accuracy check, the archive, and the QB/Xero export all light up).

    Gated by the SEED_DEMO_TOKEN env var, set out-of-band. Unset => disabled
    (404), so this can never run unless deliberately armed. Idempotent: re-seeding
    wipes and rebuilds the fixed demo tenant only — no other tenant is touched.
    """
    import os as _os
    token = _os.getenv("SEED_DEMO_TOKEN")
    if not token:
        raise HTTPException(404, "Not found")
    if not hmac.compare_digest(x_seed_token or "", token):
        raise HTTPException(403, "Invalid seed token")
    from .seed_demo import seed_realistic_demo
    return seed_realistic_demo(arrays=arrays, offtakers_per_array=offtakers_per_array)


@app.get("/admin/tenants")
def admin_list_tenants(_: None = Depends(_require_admin)):
    with SessionLocal() as db:
        ts = db.execute(select(Tenant).order_by(Tenant.created_at.desc())).scalars().all()
        # Never bulk-return full tenant_keys — a compromised admin key must not
        # become mass extension takeover. last4 is enough for support lookups;
        # full key is only shown once at create / regen.
        out = []
        for t in ts:
            key = t.tenant_key or ""
            out.append({
                "id": t.id, "name": t.name, "email": t.contact_email,
                "tenant_key_last4": key[-4:] if len(key) >= 4 else key,
                "plan": t.plan, "active": t.active, "product": t.product,
                "created_at": t.created_at.isoformat(),
            })
        return out


@app.post("/admin/tenants/link-by-email")
def admin_link_tenants_by_email(body: dict, _: None = Depends(_require_admin)):
    """Establish the cross-product sibling LINK for one email so a single
    extension install feeds BOTH the user's NEPOOL and Array Operator tenants.

    Opt-in, one email at a time, verified-email-scoped: resolves the CANONICAL
    (active, non-duplicate) tenant per product and links those bidirectionally.
    DRY-RUN unless body {"apply": true}. Reversible via /admin/tenants/unlink.
    The fan-out of captures into the sibling is SEPARATELY gated behind the
    FAN_OUT_TO_SIBLING env flag — linking alone changes no data flow until that
    flag is enabled. Admin-key gated."""
    from . import tenant_link
    email = (body or {}).get("email")
    if not email:
        raise HTTPException(400, "email required")
    return tenant_link.link_by_email(email, apply=bool((body or {}).get("apply")))


@app.post("/admin/tenants/unlink")
def admin_unlink_tenant(body: dict, _: None = Depends(_require_admin)):
    """Reverse a cross-product link: null linked_tenant_id on the given tenant
    AND its sibling. DRY-RUN unless body {"apply": true}. Admin-key gated."""
    from . import tenant_link
    tid = (body or {}).get("tenant_id")
    if not tid:
        raise HTTPException(400, "tenant_id required")
    return tenant_link.unlink_tenant(tid, apply=bool((body or {}).get("apply")))


@app.post("/admin/jobs/run")
def admin_run_jobs(_: None = Depends(_require_admin)):
    return {"ran": run_pending_jobs()}


@app.post("/admin/vendor-cred-encryption")
def admin_vendor_cred_encryption(mode: str = "dryrun", x_maint_key: str | None = Header(default=None)):
    """One-off maintenance for the vendor-credential encryption-at-rest rollout
    (PR #11). Runs IN the container (has the DB + SO_CONFIG_KEY in env). Gated by
    X-Maint-Key matching the SO_MAINT_KEY env var, which is UNSET by default so the
    endpoint is DISABLED (403) except during a deliberate rollout — set SO_MAINT_KEY
    to a throwaway value to enable it, then delete the var to disable it again.
    Lets us drive the rollout over HTTPS since project tokens cannot railway ssh.

    mode:
      dryrun        — report the plaintext rows that WOULD be encrypted (no writes; default)
      apply         — encrypt plaintext rows under SO_CONFIG_KEY
      verify        — decrypt every connection + hit its vendor read-only (live check)
      decrypt       — report the ciphertext that WOULD be unwrapped (no writes)
      decrypt-apply — unwrap ciphertext back to plaintext (rollback; needs the key)

    Returns the report dict + the captured human-readable log.
    """
    _maint = os.getenv("SO_MAINT_KEY", "")
    if not _maint or not hmac.compare_digest(x_maint_key or "", _maint):
        raise HTTPException(403, "vendor-cred-encryption maintenance is disabled")
    from api.db import engine
    from api import crypto
    from scripts import encrypt_vendor_credentials as ev
    log: list[str] = []
    out = lambda s="": log.append(str(s))   # noqa: E731 — tiny capture sink
    m = (mode or "dryrun").lower()
    try:
        if m == "verify":
            rep = ev.verify_live(engine, out=out)
        elif m == "apply":
            rep = ev.process(engine, mode="encrypt", apply=True, out=out)
        elif m == "dryrun":
            rep = ev.process(engine, mode="encrypt", apply=False, out=out)
        elif m == "decrypt":
            rep = ev.process(engine, mode="decrypt", apply=False, out=out)
        elif m == "decrypt-apply":
            rep = ev.process(engine, mode="decrypt", apply=True, out=out)
        else:
            raise HTTPException(400, "mode must be dryrun|apply|verify|decrypt|decrypt-apply")
    except HTTPException:
        raise
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — the script raises SystemExit
        # when SO_CONFIG_KEY is unset; surface every failure in the body, don't 500-blind.
        return {"ok": False, "mode": m, "encryption_enabled": crypto.encryption_enabled(),
                "error": f"{type(exc).__name__}: {exc}", "log": log}
    return {"ok": True, "mode": m, "encryption_enabled": crypto.encryption_enabled(),
            "report": rep, "log": log}


@app.post("/admin/rate-schedule/refresh")
def admin_refresh_rate_schedule(_: None = Depends(_require_admin)):
    """(Re)compute the blended RateSchedule from captured bills. Idempotent;
    measures the median $/kWh per utility × effective-window × age bucket and
    upserts rows. Run after a bill pull or when a new biennial window opens."""
    from .rate_schedule import refresh_rate_schedule
    with SessionLocal() as db:
        return refresh_rate_schedule(db)


@app.post("/admin/gmp-backfill/tenant/{tenant_id}")
def admin_gmp_backfill_tenant(
    tenant_id: str,
    window_days: int = 60,
    force_refetch: bool = False,
    _: None = Depends(_require_admin),
):
    """Run the multi-year GMP daily backfill for EVERY enabled GMP account of a
    tenant. Walks each meter backward to its history floor, stores verbatim CSV
    in the sponge (gmp_usage_raw) + derives per-day kWh (gmp_daily_generation).
    Idempotent: re-running tops up recent/missing windows only unless
    force_refetch. Returns an evidence summary (date ranges + row counts)."""
    from .jobs import gmp_daily_backfill as bf
    return bf.backfill_tenant(tenant_id, window_days=window_days, force_refetch=force_refetch)


@app.post("/admin/gmp-backfill/account/{account_id}")
def admin_gmp_backfill_account(
    account_id: int,
    window_days: int = 60,
    force_refetch: bool = False,
    _: None = Depends(_require_admin),
):
    """Run the GMP daily backfill for ONE account (meter) — full available
    history. Same storage + idempotency as the tenant runner. Useful for a
    targeted re-pull or to prove the pipeline on a single meter."""
    from .jobs import gmp_daily_backfill as bf
    return bf.backfill_account(None, account_id, window_days=window_days, force_refetch=force_refetch)


@app.post("/admin/bill-to-daily/tenant/{tenant_id}")
def admin_bill_to_daily_tenant(
    tenant_id: str,
    _: None = Depends(_require_admin),
):
    """Transform captured GMP bills into the daily-generation stream the frontend
    reads (source=bill_prorate), so parsed bills SHOW in Trends + merge with
    inverter data. Idempotent; real metered readings are never overwritten.
    Returns counts {arrays, bills_seen, days_written, days_updated,
    days_skipped_real}."""
    from .jobs.bill_to_daily import transform_tenant_bills
    return transform_tenant_bills(tenant_id)


@app.post("/admin/bill-to-daily/all")
def admin_bill_to_daily_all(_: None = Depends(_require_admin)):
    """Run the bill→daily transform across every active tenant. Same as the
    nightly 05:30 job; returns the grand-total counts."""
    from .jobs.bill_to_daily import transform_all_tenants
    return transform_all_tenants()


@app.post("/admin/inverter-history/heal")
def admin_inverter_history_heal(limit: int = 50, _: None = Depends(_require_admin)):
    """Self-healing deep-history backfill across connections whose multi-year
    history hasn't been pulled yet. Same as the nightly 04:15 job; idempotent."""
    from .jobs.inverter_history import heal_missing_history
    return heal_missing_history(limit=limit)


@app.post("/admin/inverter-history/connection/{connection_id}")
def admin_inverter_history_connection(
    connection_id: int,
    since_year: int = 2010,
    _: None = Depends(_require_admin),
):
    """Force the full multi-year history backfill for ONE inverter connection."""
    from .jobs.inverter_history import backfill_connection_history
    return backfill_connection_history(connection_id, start_year=since_year)


@app.post("/admin/array-dedup/sweep")
def admin_array_dedup_sweep(execute: bool = False, _: None = Depends(_require_admin)):
    """Detect (and optionally merge) duplicate arrays fleet-wide. DRY-RUN by
    default (execute=False) — returns what WOULD merge + what's suggested. Pass
    execute=true to actually merge the STRONG/MEDIUM tiers."""
    from .jobs.array_dedup import sweep_all_tenants
    return sweep_all_tenants(execute=execute)


@app.post("/admin/array-dedup/tenant/{tenant_id}")
def admin_array_dedup_tenant(tenant_id: str, execute: bool = False,
                             _: None = Depends(_require_admin)):
    """Detect (and optionally merge) duplicate arrays for ONE tenant. DRY-RUN by
    default. Returns the scored pairs + auto-merge/suggest split."""
    from .jobs.array_dedup import sweep_tenant
    return sweep_tenant(tenant_id, execute=execute)


@app.post("/admin/new-bill-reviews/run")
def admin_run_new_bill_reviews(
    dry_run: bool = True,
    to: str | None = None,
    _: None = Depends(_require_admin),
):
    """"Come review your next bill" sweep — when a new GMP bill has landed for an
    offtaker, email the OPERATOR a prompt to review the auto-prepared draft.

    DRY-RUN by default (dry_run=true) → returns the rendered email + the resolved
    OPERATOR recipients for every candidate WITHOUT sending or stamping dedup, so
    you can verify recipient resolution + HTML/links before any real send. Pass
    dry_run=false to actually send (deduped per bill period). Pass `to` to send a
    one-off TEST copy to that address instead of the operators (still stamps dedup
    so a real run isn't double-sent for the same period)."""
    from .jobs.new_bill_review import run_new_bill_reviews
    return run_new_bill_reviews(dry_run=dry_run, to_override=to)


# ---- root --------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    """Post-fold (2026-07-16): the old "NEPOOL Operator API" HTML landing is
    gone — anyone hitting the API root (nepooloperator.com's proxy or the
    Railway origin directly) lands on the folded product home. Permanent
    redirect only on this exact path; /health, /v1/*, /app, /admin/* are
    untouched."""
    return RedirectResponse("https://arrayoperator.com", status_code=301)


# ─── Delivery endpoints (default workbook → email) ────────────────────

from .writers import build_workbook
from .notify import send_workbook_email, send_internal_alert
from datetime import datetime as _dt


@app.get("/admin/scheduler")
def scheduler_status(_: None = Depends(_require_admin)):
    """Inspect APScheduler — confirm cron is alive on Railway."""
    sched = scheduler.scheduler  # the BackgroundScheduler instance in scheduler.py
    jobs = []
    for j in sched.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
            "trigger": str(j.trigger),
        })
    return {
        "running": sched.running,
        "jobs": jobs,
        "now": _dt.utcnow().isoformat() + "Z",
    }


@app.post("/admin/tenants/{tid}/deliver")
def deliver_now(tid: str, year: int | None = None, to: str | None = None,
                _: None = Depends(_require_admin)):
    """Force-run the workbook delivery for one tenant. Every tenant gets
    the default arrays×months workbook. Pass ?to=someone@example.com to
    override recipient (admin testing). Workbook is built in a tempdir
    that is wiped after send — no persistent disk dependency."""
    from .delivery import deliver_for_tenant
    try:
        result = deliver_for_tenant(tid, year=year, override_to=to, triggered_by="ops")
    except ValueError as e:
        raise HTTPException(404, str(e))
    if not result.get("ok"):
        raise HTTPException(500, f"Delivery failed: {result.get('error', 'see logs')}")
    return result


@app.post("/admin/test-email")
def test_email(to: str | None = None, _: None = Depends(_require_admin)):
    """Fire a 1-line test email + return the Resend error if it fails.
    Use ?to=ford.genereaux@gmail.com to override recipient."""
    import os as _os
    from .notify import _send_via_resend, RESEND_API_KEY, FROM_ADDRESS, INTERNAL_ALERT_TO
    target = to or INTERNAL_ALERT_TO
    ok = _send_via_resend(
        to=target,
        subject="NEPOOL Operator email pipeline test",
        html="<p>If you can read this, Resend → your inbox works.</p>",
        text="If you can read this, Resend → your inbox works.",
    )
    return {
        "ok": ok,
        "to": target,
        "from": FROM_ADDRESS,
        "has_api_key": bool(RESEND_API_KEY),
        "last_error": getattr(_send_via_resend, "_last_error", None),
    }


# ─── Onboarding SPA (React + Vite, built to web/onboarding/dist) ──────────
# Served at /onboarding/*. Mounted LAST so it can never shadow the JSON API
# routes above (/v1/*, /admin/*, /, /health) — a Mount only matches paths
# under its own prefix anyway, but registering it last keeps that obvious.

class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html on a 404.

    The wizard uses React Router's BrowserRouter, so deep links like
    /onboarding/clients have no corresponding file on disk. On a hard refresh
    plain StaticFiles would 404; here we serve index.html instead and let the
    client-side router resolve the path.

    Per spec, ANY unmatched /onboarding/* path returns index.html — including a
    genuinely-missing asset (e.g. a stale /onboarding/assets/*.js after a bad
    build), which then fails to parse client-side rather than 404ing. That only
    happens on a broken deploy, so the simpler blanket fallback is the trade.

    CACHING: the SPA shell (index.html) must NEVER be cached, or a browser keeps
    loading the previous deploy's hashed chunk references and users run stale code
    after every deploy (e.g. a fixed auth bug that "still" reproduces because the
    old index.html is cached). We stamp `Cache-Control: no-cache` on index.html so
    it's always revalidated, while the content-hashed assets under /assets/ stay
    immutable-cacheable (their filenames change every build, so caching them
    forever is correct and fast).
    """

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                response = await super().get_response("index.html", scope)
            else:
                raise
        # index.html (the SPA shell, served directly or as the deep-link / 404
        # fallback) must always be revalidated so a new deploy is picked up
        # immediately. Hashed assets are immutable and keep their long cache.
        is_index = path in ("", ".", "/", "index.html") or path.endswith("/index.html")
        # The fallback path above re-served index.html even when `path` was an
        # unmatched route, so also treat any text/html response as the shell.
        media_type = response.headers.get("content-type", "")
        if is_index or media_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# ---- v0.6.7: per-change preview bundles (MC Lens-Picker live iframes) -------
#
# MC uploads a built dist/ as a tar.gz after each Worker run. The bundle is
# extracted to PREVIEW_BUNDLES_DIR/<change_id>/dist/ and served at
# /accounts/preview/<change_id>/. Cleanup via DELETE /admin/preview/<change_id>.

PREVIEW_BUNDLES_DIR = pathlib.Path(os.path.dirname(__file__)) / "preview_bundles"


def _preview_bundle_path(change_id: str) -> pathlib.Path:
    """Return the dist/ directory for a given change_id."""
    safe = change_id.replace("/", "").replace("..", "")  # no traversal
    return PREVIEW_BUNDLES_DIR / safe / "dist"


@app.post("/admin/preview/upload")
async def preview_upload(
    request: Request,
    change_id: str,
    _: None = Depends(_require_admin),
):
    """Accept a tar.gz of a dist/ directory from MC's preview_uploader.

    The archive must have a top-level `dist/` directory. Files are extracted to
    PREVIEW_BUNDLES_DIR/<change_id>/dist/ and served at /accounts/preview/<change_id>/.

    Returns 200 + {"ok": true, "preview_url": "..."} on success.
    """
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "empty body — expected a tar.gz of the dist/ directory")

    dest = _preview_bundle_path(change_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            # Security: only extract safe members (no absolute paths, no ..)
            safe_members = [
                m for m in tar.getmembers()
                if not os.path.isabs(m.name) and ".." not in m.name
            ]
            tar.extractall(path=str(dest.parent), members=safe_members)
    except (tarfile.TarError, EOFError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(400, f"invalid tar.gz: {exc}") from exc

    if not dest.exists():
        raise HTTPException(422, "tar.gz did not contain a dist/ directory at its root")

    return {"ok": True, "change_id": change_id,
            "preview_url": f"/accounts/preview/{change_id}/"}


@app.delete("/admin/preview/{change_id}")
def preview_delete(
    change_id: str,
    _: None = Depends(_require_admin),
):
    """Clean up a preview bundle after the founder ships or declines the lens."""
    bundle_dir = PREVIEW_BUNDLES_DIR / change_id.replace("/", "").replace("..", "")
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir, ignore_errors=True)
    return {"ok": True, "change_id": change_id}


# Dynamic preview file serving — /accounts/preview/<change_id>/<path>
# Serves the per-change bundle as a SPA (index.html fallback for deep links).
@app.get("/accounts/preview/{change_id}/{file_path:path}")
@app.get("/accounts/preview/{change_id}")
def accounts_preview(change_id: str, file_path: str = ""):
    """Serve the live preview bundle for a change. Acts as a mini-SPA with
    index.html fallback (same behaviour as SPAStaticFiles)."""
    dist = _preview_bundle_path(change_id)
    if not dist.exists():
        raise HTTPException(404, f"no preview bundle for change {change_id}")

    # Normalise: empty path or directory → index.html
    target = (dist / file_path) if file_path else dist / "index.html"
    if target.is_dir():
        target = target / "index.html"

    # Containment check on the fully-resolved real paths so symlinks and ".."
    # / Unicode-normalised traversal can't escape the bundle directory. A plain
    # str.startswith on the unresolved path was bypassable (CVE-class).
    dist_real = dist.resolve()
    try:
        target_real = target.resolve()
        contained = target_real == dist_real or dist_real in target_real.parents
    except OSError:
        contained = False

    if not contained or not target_real.exists():
        # SPA fallback: serve index.html for any unresolved path
        index = dist / "index.html"
        if index.exists():
            return FileResponse(str(index), media_type="text/html")
        raise HTTPException(404, "preview index.html not found")

    return FileResponse(str(target_real))


# Each SPA's build is copied into api/ before commit because Railway's Railpack
# builder only ships directories it recognizes — web/ at the repo root was being
# dropped. build_onboarding.sh and build_app.sh do the copy.
#   /onboarding ← api/onboarding_dist  (web/onboarding/dist)
#   /app        ← api/app_dist         (web/app/dist — customer dashboard)
#   /accounts   ← api/app_dist         (alias for the customer SPA — production iframe)
_SPA_MOUNTS = [
    ("/onboarding", "onboarding_dist", "build_onboarding.sh", "web/onboarding"),
    ("/app", "app_dist", "build_app.sh", "web/app"),
    ("/accounts", "app_dist", "build_app.sh", "web/app"),  # v0.6.7 — production iframe alias
]

for _prefix, _dirname, _build_script, _src in _SPA_MOUNTS:
    _dist = os.path.normpath(os.path.join(os.path.dirname(__file__), _dirname))
    if os.path.isdir(_dist):
        app.mount(
            _prefix,
            SPAStaticFiles(directory=_dist, html=True),
            name=_prefix.lstrip("/"),
        )
        log.info("Mounted SPA %s from %s", _prefix, _dist)
    else:
        # Local dev without a frontend build — don't crash startup, just warn.
        log.warning(
            "SPA %s not mounted: %s does not exist. Run `./%s` to enable it.",
            _prefix, _dist, _build_script,
        )
