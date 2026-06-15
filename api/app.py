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
import io, os, pathlib, secrets, shutil, json, logging, tarfile
from datetime import datetime
from typing import Any
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select, func, or_
from .db import init_db, SessionLocal
from .models import Tenant, Client, UtilityAccount, UtilitySession, Bill, Job, Array, now
from .fuels import normalize_fuel
from .adapters import get_adapter, is_smarthub_provider
from .sync_filter import classify_residential
import re as _re

_SYNC_EMAIL_RE = _re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


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
from .daily_generation import router as daily_generation_router
from .array_owners import router as array_owners_router
from .warranty_claims import router as warranty_claims_router
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

app = FastAPI(title="NEPOOL Operator API", version="1.0.0")

# Error monitoring (launch readiness). Initialized as early as possible so any
# startup or request error is captured. No-op unless SENTRY_DSN is set, so dev +
# tests are unaffected. This single backend serves BOTH products (NEPOOL Operator
# and Array Operator), so this one init covers server-side errors system-wide.
from .observability import init_sentry, capture_exception  # noqa: E402
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
# Phase 2 daily generation CSV ingest + coverage.
app.include_router(daily_generation_router)

app.include_router(array_owners_router)
app.include_router(warranty_claims_router)
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
if _SO_DEV_ENABLED:
    import logging
    logging.getLogger("uvicorn.error").warning(
        "SO_DEV_ENABLED=1 — /v1/dev/* seed/wipe endpoints are LIVE"
    )


@app.on_event("startup")
def _startup():
    init_db()
    scheduler.start()
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


@app.get("/health")
def health():
    # Additive, non-secret readiness diagnostics so we can verify at a glance that
    # prod is on Postgres (not the SQLite fallback) and that email/billing are
    # wired. Booleans only — no keys or values are exposed. Still returns ok:true
    # so the Railway healthcheck contract is unchanged.
    try:
        from .db import engine
        dialect = engine.dialect.name
        pool_max = engine.pool.size() + engine.pool._max_overflow if dialect != "sqlite" else None
    except Exception:
        dialect, pool_max = "unknown", None
    return {
        "ok": True,
        "service": "solar-operator-api",
        "db": dialect,                       # "postgresql" = good; "sqlite" = NOT production-ready
        "db_pool_max": pool_max,
        "email_configured": bool(os.getenv("RESEND_API_KEY")),
        "stripe_configured": bool(os.getenv("STRIPE_SECRET_KEY")),
        "stripe_array_price_set": bool(os.getenv("STRIPE_ARRAY_PRICE_ID")),
        "sentry_configured": bool(os.getenv("SENTRY_DSN")),
        "web_concurrency": int(os.getenv("WEB_CONCURRENCY", "1")),
    }


@app.post("/v1/extension/heartbeat")
def extension_heartbeat(authorization: str | None = Header(default=None)):
    """Lightweight periodic ping from the Chrome extension so the onboarding
    screen can distinguish 'extension installed and active' from 'not detected.'
    Called by background.js every 60s when on a GMP tab. Returns the server
    timestamp so the extension can show clock-skew warnings if needed."""
    tenant = tenant_from_bearer(authorization)
    require_not_demo(tenant)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if t:
            t.extension_heartbeat_at = now()
            db.commit()
    return {"ok": True, "at": datetime.utcnow().isoformat()}


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
        # just the latest. Re-capturing the same login upserts its row in place;
        # a capture with no single customer identity is stored unkeyed and
        # selection falls back to latest-per-provider (legacy behavior).
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
        existing_sess = None
        if session_customer:
            existing_sess = db.execute(
                select(UtilitySession)
                .where(UtilitySession.tenant_id == tenant.id,
                       UtilitySession.provider == normalized["provider"],
                       UtilitySession.customer_number == session_customer)
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
                        # Resurrect a soft-deleted Array with the same
                        # (tenant_id, name) — the uq_array_per_tenant
                        # unique constraint doesn't exclude deleted_at.
                        ghost_arr = db.execute(
                            select(Array).where(
                                Array.tenant_id == tenant.id,
                                Array.name == arr_name,
                                Array.deleted_at.is_not(None),
                            ).limit(1)
                        ).scalar_one_or_none()
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
                            ghost_arr = db.execute(
                                select(Array).where(
                                    Array.tenant_id == tenant.id,
                                    Array.name == arr_name,
                                    Array.deleted_at.is_not(None),
                                ).limit(1)
                            ).scalar_one_or_none()
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

            for b_raw in (normalized.get("bills_raw") or []):
                b = _vec_parse_bill(b_raw)
                acct_no = b.get("account_id")
                acct_id = acct_map.get(acct_no)
                if not acct_id or not b.get("billing_date"):
                    continue
                doc_no = b.get("bill_uuid") or b.get("pdf_url") or b["billing_date"].strftime("VEC-%Y-%m-%d")
                exists = db.execute(
                    select(Bill).where(
                        Bill.account_id == acct_id,
                        Bill.document_number == doc_no,
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                # Attach usage from the same billing month, if available.
                # API-captured bills (v1.6.0) carry kWh + meter-read period
                # inline — prefer those; usage-explorer rows remain the
                # fallback for DOM-scraped captures.
                u = usage_by_acct.get(acct_no, {}).get(b["billing_date"].strftime("%Y-%m"))
                _ps = b.get("period_start") or (u["period_start"] if u else None)
                _pe = b.get("period_end") or (u["period_end"] if u else None)
                _kwh = b.get("kwh")
                if _kwh is None and u:
                    _kwh = u["kwh"]
                db.add(Bill(
                    tenant_id=tenant.id,
                    account_id=acct_id,
                    bill_date=b["billing_date"],
                    period_start=_ps,
                    period_end=_pe,
                    kwh_generated=int(_kwh) if _kwh is not None else None,
                    document_number=doc_no,
                    pdf_path=b.get("pdf_url"),
                    parse_status="parsed" if _kwh is not None else "partial",
                ))

        try:
            ctx.flush(db)
        except Exception:
            log.warning("CaptureEvent flush failed for tenant %s", tenant.id, exc_info=True)
        db.commit()

    # Fire a bill-pull immediately so the operator sees data the moment they
    # finish logging into GMP. The 6h scheduler tick is the long-tail safety
    # net; this is the "trust the system" moment. Best-effort, never blocks
    # the response.
    try:
        from threading import Thread
        from .worker import pull_bills_for_tenant
        def _bg_pull(tid: str):
            try:
                pull_bills_for_tenant(tid)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "post-/v1/sync pull_bills_for_tenant failed for %s", tid)
        Thread(target=_bg_pull, args=(tenant.id,), daemon=True).start()
    except Exception:
        # Never let pull-kickoff failures break the sync response.
        import logging
        logging.getLogger(__name__).exception("failed to kick off post-sync pull")

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
    if x_admin_key != ADMIN_API_KEY:
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


@app.get("/admin/tenants")
def admin_list_tenants(_: None = Depends(_require_admin)):
    with SessionLocal() as db:
        ts = db.execute(select(Tenant).order_by(Tenant.created_at.desc())).scalars().all()
        return [
            {"id": t.id, "name": t.name, "email": t.contact_email,
             "tenant_key": t.tenant_key, "plan": t.plan, "active": t.active,
             "created_at": t.created_at.isoformat()}
            for t in ts
        ]


@app.post("/admin/jobs/run")
def admin_run_jobs(_: None = Depends(_require_admin)):
    return {"ran": run_pending_jobs()}


# ---- root --------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root():
    return """
<!DOCTYPE html><html><head><title>NEPOOL Operator API</title>
<style>body{font-family:Georgia,serif;max-width:680px;margin:40px auto;padding:0 20px;color:#222}
h1{color:#2e6b3a;border-bottom:2px solid #2e6b3a;padding-bottom:8px}
code{background:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:13px}
.endpoint{background:#eef3ec;border-left:3px solid #2e6b3a;padding:8px 12px;margin:6px 0;font-family:ui-monospace,monospace;font-size:13px}
</style></head><body>
<h1>NEPOOL Operator API</h1>
<p>Backend for the Chrome extension. Receives captured utility sessions, pulls bills, drafts reports.</p>
<h3>Ingest</h3>
<div class="endpoint">POST /v1/sync — Chrome extension target</div>
<h3>Tenant</h3>
<div class="endpoint">GET  /v1/tenants/{id}/status</div>
<div class="endpoint">GET  /v1/tenants/{id}/bills</div>
<div class="endpoint">POST /v1/tenants/{id}/pull — force a pull-bills run</div>
<h3>Admin</h3>
<div class="endpoint">POST /admin/tenants    {"name":"...", "contact_email":"..."}</div>
<div class="endpoint">GET  /admin/tenants</div>
<div class="endpoint">POST /admin/jobs/run</div>
<p style="margin-top:30px;color:#888;font-size:13px">NEPOOL Operator · hundreds of utilities supported nationwide · multi-tenant</p>
</body></html>
"""


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
        "api_key_prefix": RESEND_API_KEY[:7] if RESEND_API_KEY else None,
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
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


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

    if not target.exists() or not str(target).startswith(str(dist)):
        # SPA fallback: serve index.html for any unresolved path
        index = dist / "index.html"
        if index.exists():
            return FileResponse(str(index), media_type="text/html")
        raise HTTPException(404, "preview index.html not found")

    return FileResponse(str(target))


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
