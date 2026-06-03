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
from sqlalchemy import select, func
from .db import init_db, SessionLocal
from .models import Tenant, Client, UtilityAccount, UtilitySession, Bill, Job, Array, now
from .adapters import get_adapter
from .worker import pull_bills_for_tenant, run_pending_jobs
from .signup import router as signup_router
from .account import router as account_router
from .onboarding import router as onboarding_router
from . import scheduler

log = logging.getLogger("solar_operator.app")

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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(signup_router)
app.include_router(account_router)
# New 5-screen onboarding flow. Legacy signup_router stays mounted so any
# in-flight /v1/signup checkouts still complete.
app.include_router(onboarding_router)


@app.on_event("startup")
def _startup():
    init_db()
    scheduler.start()


@app.get("/health")
def health():
    return {"ok": True, "service": "solar-operator-api"}


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
                row.customer_number = a.get("customer_number") or row.customer_number
                row.nickname = a.get("nickname") or row.nickname
                row.service_address = a.get("service_address") or row.service_address
                row.extra = {**(row.extra or {}), **extra}
                row.last_seen = now()

        # Flush so the accounts just upserted above have IDs and are findable
        # by the autopop query below (autoflush is off on this session).
        db.flush()

        # ── GMP auto-populate (onboarding wizard Screen 4) ───────────────
        # If this capture's GMP login email matches a Client that opted into
        # autopop, append/link Arrays automatically so the operator doesn't
        # have to type them in by hand.
        #
        # ⚠️⚠️  LOUD WARNING — MULTI-ACCOUNT-PER-ARRAY CASE  ⚠️⚠️
        # Autopop creates ONE Array per GMP account. Real-world arrays that
        # SUM several sub-meters into a single logical array CANNOT be detected
        # here. Bruce's "Starlake" = 3 GMP accounts → 1 array (with
        # bill_offset_months=0). Autopop will instead create 3 SEPARATE arrays.
        # The operator MUST merge those duplicates manually via the dashboard
        # afterward — do NOT attempt to guess merges from account metadata.
        user_email = (normalized.get("user") or {}).get("email")
        if user_email:
            clients = db.execute(
                select(Client)
                .where(
                    Client.tenant_id == tenant.id,
                    Client.gmp_autopopulate.is_(True),
                    func.lower(Client.gmp_email) == user_email.strip().lower(),
                )
                .order_by(Client.id)
            ).scalars().all()
            if clients:
                # gmp_email is expected to map to a single Client; if more than
                # one matches (misconfiguration), the lowest-id Client owns the
                # arrays and the rest just get their last_sync timestamp bumped.
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
                    if acct is None or acct.array_id is not None:
                        # Already linked → idempotent: don't create a duplicate.
                        continue
                    arr = Array(
                        tenant_id=tenant.id,
                        client_id=owner.id,
                        name=a.get("nickname") or acct_no,
                        bill_offset_months=1,  # GMP default (prior-month bill)
                    )
                    db.add(arr)
                    db.flush()  # assign arr.id before linking
                    acct.array_id = arr.id
                for c in clients:
                    c.gmp_last_sync_at = now()

        db.commit()

    return {
        "ok": True,
        "tenant": tenant.id,
        "accounts": len(normalized["accounts"]),
        "token_expires_at": normalized["auth"].get("apiTokenExpires"),
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
        bills_count = db.execute(select(Bill).where(Bill.tenant_id == tid)).scalars().all()
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
            "bills_count": len(bills_count),
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

@app.post("/admin/tenants")
def admin_create_tenant(body: dict):
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
def admin_list_tenants():
    with SessionLocal() as db:
        ts = db.execute(select(Tenant).order_by(Tenant.created_at.desc())).scalars().all()
        return [
            {"id": t.id, "name": t.name, "email": t.contact_email,
             "tenant_key": t.tenant_key, "plan": t.plan, "active": t.active,
             "created_at": t.created_at.isoformat()}
            for t in ts
        ]


@app.post("/admin/jobs/run")
def admin_run_jobs():
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
def scheduler_status():
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
def deliver_now(tid: str, year: int | None = None, to: str | None = None):
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
def test_email(to: str | None = None):
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


_ONBOARDING_DIST = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "web", "onboarding", "dist")
)

if os.path.isdir(_ONBOARDING_DIST):
    app.mount(
        "/onboarding",
        SPAStaticFiles(directory=_ONBOARDING_DIST, html=True),
        name="onboarding",
    )
    log.info("Mounted onboarding SPA from %s", _ONBOARDING_DIST)
else:
    # Local dev without a frontend build — don't crash startup, just warn.
    # Build it with: cd web/onboarding && npm ci && npm run build
    log.warning(
        "Onboarding SPA not mounted: %s does not exist. "
        "Run `cd web/onboarding && npm ci && npm run build` to enable /onboarding.",
        _ONBOARDING_DIST,
    )
