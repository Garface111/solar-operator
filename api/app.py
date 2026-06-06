"""
Solar Operator — FastAPI app.

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
import os, secrets, json, logging
from datetime import datetime
from typing import Any
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select, func, or_
from .db import init_db, SessionLocal
from .models import Tenant, Client, UtilityAccount, UtilitySession, Bill, Job, Array, now
from .adapters import get_adapter
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
            return str(cname).strip()[:200]

    # 2. Portal user profile name (account holder display name)
    holder = (
        (user_dict.get("name") or "").strip()
        or (user_dict.get("fullName") or "").strip()
        or (user_dict.get("display_name") or "").strip()
        or (user_dict.get("displayName") or "").strip()
    )
    if holder:
        return holder[:200]

    # 3. Local-part of email, de-dotted + title-cased
    if user_email and "@" in user_email:
        local = user_email.split("@")[0]
        cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ")
        result = cleaned.strip().title()
        return result[:200] if result else user_email[:200]

    # 4. Username
    if user_username:
        return user_username[:200]

    return "New client"
from .worker import pull_bills_for_tenant, run_pending_jobs
# v1.1.0: api/signup.py was renamed to api/_legacy_signup.py and unmounted.
# Its Stripe webhook moved to api/stripe_webhook.py (still mounted below).
from .stripe_webhook import router as stripe_webhook_router
from .account import router as account_router
from .onboarding import router as onboarding_router
from .ingest import router as ingest_router
from .nepool_assign import router as nepool_router
from .resend_webhook import router as resend_webhook_router
from .sandbox import router as sandbox_router
from .dev_sandbox import router as dev_sandbox_router, DEV_ENABLED as _SO_DEV_ENABLED
from . import scheduler

log = logging.getLogger("solar_operator.app")

# Maps provider code → which Client columns drive auto-populate for that provider.
# Using string attribute names for setattr (last_sync_attr) and column descriptors
# for SQLAlchemy where-clause matching (email_col, username_col, autopop_col).
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
    "vec": {
        "email_col":          Client.vec_email,
        "username_col":       Client.vec_username,
        "autopop_col":        Client.vec_autopopulate,
        "last_sync_attr":     "vec_last_sync_at",
        "email_attr":         "vec_email",
        "username_attr":      "vec_username",
        "autopop_attr":       "vec_autopopulate",
        "bill_offset_months": 0,   # VEC bills represent the same month
    },
}

app = FastAPI(title="Solar Operator API", version="1.0.0")

# CORS — allow the marketing site to call /v1/signup, and any Chrome
# extension to call /v1/sync (extensions identify with chrome-extension://
# origins; we authenticate them per-request via tenant_key bearer token).
ALLOWED_ORIGINS = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "https://solaroperator.org,https://www.solaroperator.org,http://localhost:3000"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_origin_regex=r"^chrome-extension://[a-z0-9]+$",
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# v1.1.0: replaced by onboarding.router. Webhook handler still active via the new onboarding flow.
# app.include_router(signup_router)
app.include_router(stripe_webhook_router)
app.include_router(account_router)
# New 5-screen onboarding flow.
app.include_router(onboarding_router)
# V4: AI spreadsheet ingest for arrays + NEPOOL IDs.
app.include_router(ingest_router)
# AI-assisted NEPOOL ID assignment from spreadsheet (pure assignment, no array creation).
app.include_router(nepool_router)
# W2-6: Resend delivery webhook → per-client delivery health.
app.include_router(resend_webhook_router)
# Sandbox canvas: client graph visualization + position persistence.
app.include_router(sandbox_router)
# Dev-only sandbox helpers. Mounted but each route guards on SO_DEV_ENABLED;
# the /status route is always reachable so the SPA can decide what to render.
app.include_router(dev_sandbox_router)
if _SO_DEV_ENABLED:
    import logging
    logging.getLogger("uvicorn.error").warning(
        "SO_DEV_ENABLED=1 — /v1/dev/* seed/wipe endpoints are LIVE"
    )


@app.on_event("startup")
def _startup():
    init_db()
    scheduler.start()


@app.get("/health")
def health():
    return {"ok": True, "service": "solar-operator-api"}


@app.post("/v1/extension/heartbeat")
def extension_heartbeat(authorization: str | None = Header(default=None)):
    """Lightweight periodic ping from the Chrome extension so the onboarding
    screen can distinguish 'extension installed and active' from 'not detected.'
    Called by background.js every 60s when on a GMP tab. Returns the server
    timestamp so the extension can show clock-skew warnings if needed."""
    tenant = tenant_from_bearer(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if t:
            t.extension_heartbeat_at = now()
            db.commit()
    return {"ok": True, "at": datetime.utcnow().isoformat()}


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
    payload = await request.json()
    adapter = get_adapter(payload.get("provider", "gmp"))
    normalized = adapter.parse_extension_payload(payload)

    with SessionLocal() as db:
        # store the session
        expires_at = None
        if normalized["auth"].get("apiTokenExpires"):
            try:
                expires_at = datetime.fromisoformat(normalized["auth"]["apiTokenExpires"].replace("Z","+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        sess = UtilitySession(
            tenant_id=tenant.id,
            provider=normalized["provider"],
            api_token=normalized["auth"].get("apiToken", ""),
            refresh_token=normalized["auth"].get("refreshToken"),
            expires_at=expires_at,
            raw_payload={"user": normalized["user"]},
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

        # Flush so the accounts just upserted above have IDs and are findable
        # by the autopop query below (autoflush is off on this session).
        db.flush()

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
                            )
                            db.add(arr)
                        db.flush()  # assign arr.id before linking
                        acct.array_id = arr.id
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

                    if user_email or user_username:
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
                            chosen_name = (display_name or "New client")[:200]
                            ghost = db.execute(
                                select(Client).where(
                                    Client.tenant_id == tenant.id,
                                    Client.name == chosen_name,
                                    Client.deleted_at.is_not(None),
                                ).limit(1)
                            ).scalar_one_or_none()
                            if ghost is not None:
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
                            # Same defensive check as the autopop branch:
                            # array_id may point at a soft-deleted Array
                            # from a prior captured-then-deleted cycle.
                            if acct.array_id is not None:
                                existing_arr = db.get(Array, acct.array_id)
                                if existing_arr is None or existing_arr.deleted_at is not None:
                                    acct.array_id = None
                                else:
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
                                )
                                db.add(arr)
                            db.flush()
                            acct.array_id = arr.id
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

        # ── VEC bill/usage persistence (extension-scraped) ──────────────
        # GMP has a separate worker that pulls bill PDFs server-side and
        # parses them. VEC's portal uses cookie-only auth, so the extension
        # is the only path to data; the extension POSTs bills_raw + usage_raw
        # in the payload and we persist them here.
        if provider == "vec":
            from .adapters.vec import parse_bill as _vec_parse_bill, parse_usage as _vec_parse_usage
            # Build an account-number → UtilityAccount.id map from this tenant's accounts
            acct_map = {
                r.account_number: r.id
                for r in db.execute(
                    select(UtilityAccount).where(
                        UtilityAccount.tenant_id == tenant.id,
                        UtilityAccount.provider == "vec",
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
                # Attach usage from the same billing month, if available
                u = usage_by_acct.get(acct_no, {}).get(b["billing_date"].strftime("%Y-%m"))
                db.add(Bill(
                    tenant_id=tenant.id,
                    account_id=acct_id,
                    bill_date=b["billing_date"],
                    period_start=u["period_start"] if u else None,
                    period_end=u["period_end"] if u else None,
                    kwh_generated=int(u["kwh"]) if u else None,
                    document_number=doc_no,
                    pdf_path=b.get("pdf_url"),
                    parse_status="parsed" if u else "partial",
                ))

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

    return {
        "ok": True,
        "tenant": tenant.id,
        "accounts": len(normalized["accounts"]),
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
    if tenant.id != tid:
        raise HTTPException(403, "tenant mismatch")
    result = pull_bills_for_tenant(tid)
    return result


# ---- admin -------------------------------------------------------------

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


def _require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Guard admin endpoints with ADMIN_API_KEY. If the env var is unset (local
    dev), all requests are allowed so the local tooling keeps working. In prod
    the key must match."""
    if ADMIN_API_KEY and x_admin_key != ADMIN_API_KEY:
        raise HTTPException(403, "Invalid or missing X-Admin-Key")


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
<!DOCTYPE html><html><head><title>Solar Operator API</title>
<style>body{font-family:Georgia,serif;max-width:680px;margin:40px auto;padding:0 20px;color:#222}
h1{color:#2e6b3a;border-bottom:2px solid #2e6b3a;padding-bottom:8px}
code{background:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:13px}
.endpoint{background:#eef3ec;border-left:3px solid #2e6b3a;padding:8px 12px;margin:6px 0;font-family:ui-monospace,monospace;font-size:13px}
</style></head><body>
<h1>Solar Operator API</h1>
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
<p style="margin-top:30px;color:#888;font-size:13px">Solar Operator v0.1.0 · single-utility (GMP) · multi-tenant ready</p>
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
        subject="Solar Operator email pipeline test",
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


# Each SPA's build is copied into api/ before commit because Railway's Railpack
# builder only ships directories it recognizes — web/ at the repo root was being
# dropped. build_onboarding.sh and build_app.sh do the copy.
#   /onboarding ← api/onboarding_dist  (web/onboarding/dist)
#   /app        ← api/app_dist         (web/app/dist — customer dashboard)
_SPA_MOUNTS = [
    ("/onboarding", "onboarding_dist", "build_onboarding.sh", "web/onboarding"),
    ("/app", "app_dist", "build_app.sh", "web/app"),
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
