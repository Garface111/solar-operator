"""FastAPI app: shared-password auth, dashboard, JSON API, chat endpoint."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from . import (
    accounts_terms,
    config,
    events,
    goals as goals_lib,
    realestate,
    vault,
    watchpoints as watchpoints_lib,
)
from .connectors import email_harvest, simplefin
from .connectors.csv_import import import_csv
from .connectors.ofx_import import import_ofx
from .db import init_db, session_scope
from .ingest import MANUAL_KINDS, normalize_manual_balance, upsert_account
from .messaging import sms
from .messaging import thread as sms_thread
from .intelligence.insights import net_worth, net_worth_history, spending_summary, upcoming_bills
from .models import (
    Account,
    AgentAction,
    BalanceSnapshot,
    ChatMessage,
    Comp,
    Document,
    MemoryNote,
    Property,
    Rule,
    RuleFiring,
    SyncLog,
    Transaction,
)
from .rules.engine import RULE_KINDS
from .scheduler import run_rules_once, start_background_tasks

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO — and the SimpleFIN access URL carries the
# household's bank credential in its path, so that would write a working secret
# into the log file on every sync. Warnings and errors still come through.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "bankai_session"


SESSION_MAX_AGE = 60 * 60 * 24 * 90  # 90 days, enforced server-side, not just via cookie max-age


def _sign(issued: int) -> str:
    return hmac.new(
        config.SESSION_SECRET.encode(),
        f"bankai-authenticated:{issued}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _mint_token() -> str:
    """A session token stamped with the current time. The signature binds the
    issue time, so the token carries its own expiry and rotating SESSION_SECRET
    invalidates every outstanding token at once (the only way to revoke before)."""
    issued = int(time.time())
    return f"{issued}.{_sign(issued)}"


def require_auth(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME, "")
    if not config.APP_PASSWORD:
        raise HTTPException(500, "APP_PASSWORD is not configured")
    issued_str, _, sig = token.partition(".")
    if not sig or not issued_str.isdigit():
        raise HTTPException(401, "Not authenticated")
    if not hmac.compare_digest(sig, _sign(int(issued_str))):
        raise HTTPException(401, "Not authenticated")
    if int(time.time()) - int(issued_str) > SESSION_MAX_AGE:
        raise HTTPException(401, "Session expired — sign in again")
    return token


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = start_background_tasks()
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="BankAI", lifespan=lifespan)


# Security headers on every response. The dashboard renders bank- and
# email-derived text; connect-src/img-src 'self' means that even if an escaping
# slip ever let script run, it could not exfiltrate the household's data to
# another origin. script/style keep 'unsafe-inline' because the dashboard ships
# inline handlers — the output-encoding in index.html is the primary XSS defense;
# this is the egress + clickjacking backstop.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


# Login throttle: localhost-only plus a strong password already make brute force
# impractical, but an unbounded /api/login is still worth closing. One global
# window (single household, single process) backs everyone off briefly after a
# burst of failures rather than allowing unlimited guesses.
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 10
_login_failures: list[float] = []


class LoginBody(BaseModel):
    password: str


class ChatBody(BaseModel):
    message: str
    speaker: str = "Dashboard"


class RuleBody(BaseModel):
    name: str
    kind: str
    params: dict = {}
    message: str = ""


class ManualAccountBody(BaseModel):
    name: str
    kind: str
    balance: float
    owner: str = "joint"


class PropertyBody(BaseModel):
    account_id: str
    street: str
    city: str
    state: str
    zip_code: str = ""
    sqft: int | None = None
    beds: float | None = None
    baths: float | None = None
    year_built: int | None = None
    auto_update: bool = True


class GoalBody(BaseModel):
    name: str
    target_amount: float
    category: str = "savings"
    target_date: str = ""
    linked_account_id: str | None = None
    starting_amount: float | None = None
    note: str = ""


class CompBody(BaseModel):
    address: str
    price: float
    status: str = "sold"
    sale_date: str = ""
    sqft: int | None = None
    beds: float | None = None
    baths: float | None = None
    distance_miles: float | None = None


def _sec_event(kind: str, severity: str = "info", actor: str = "system", summary: str = "") -> None:
    """Best-effort write to Sentinel's tamper-evident ledger. Wrapped so a failed
    audit write can never break the request it is recording."""
    try:
        from .security import sentinel

        with session_scope() as s:
            sentinel.record_event(s, kind=kind, severity=severity, actor=actor, summary=summary)
    except Exception:
        pass


@app.post("/api/login")
def login(body: LoginBody):
    now = time.time()
    global _login_failures
    _login_failures = [t for t in _login_failures if now - t < _LOGIN_WINDOW_SECONDS]
    if len(_login_failures) >= _LOGIN_MAX_FAILURES:
        _sec_event("login_throttled", "warning", "unknown", "login blocked — too many recent failures")
        raise HTTPException(429, "Too many attempts — wait a few minutes and try again")
    if not config.APP_PASSWORD or not hmac.compare_digest(body.password, config.APP_PASSWORD):
        _login_failures.append(now)
        _sec_event("login_failed", "notice", "unknown", "wrong password")
        raise HTTPException(401, "Wrong password")
    _login_failures.clear()
    _sec_event("login_ok", "info", "household", "successful sign-in")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME,
        _mint_token(),
        httponly=True,
        secure=True,  # localhost is a secure context, so browsers still accept it
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return resp


@app.get("/api/security")
def security_report(_: str = Depends(require_auth)):
    """The Defense panel: current posture, ledger integrity, and recent security
    events. Read-only — nothing here changes a control."""
    from .security import sentinel

    with session_scope() as session:
        return sentinel.report(session)


@app.post("/api/logout")
def logout():
    """Clear the session cookie. There was no way to sign out before."""
    _sec_event("logout", "info", "household", "signed out")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/health")
def health():
    # Public liveness only. This used to return the LLM backend, model, and the
    # full xAI auth status (account email, team id, on-disk auth-file paths of
    # other agents on the box) with no authentication — an info-disclosure to
    # anything that could reach the port. Diagnostics moved to /api/health/detail.
    return {"ok": True}


@app.get("/api/health/detail")
def health_detail(_: str = Depends(require_auth)):
    from . import config
    from .xai_auth import xai_auth_status

    return {
        "ok": True,
        "llm_backend": config.LLM_BACKEND,
        "grok_model": config.GROK_MODEL,
        "xai": xai_auth_status(),
    }


EVENT_POLL_SECONDS = 1.5
EVENT_HEARTBEAT_SECONDS = 20


@app.get("/api/events")
async def event_stream(request: Request, _: str = Depends(require_auth)):
    """Server-Sent Events: names the topics whose data moved, so an open
    dashboard reloads only the affected panels — no manual refresh, and it
    reflects writes from every process (agent tools, scheduler, email, the
    other spouse's browser), not just this one."""

    def _read() -> dict:
        with session_scope() as session:
            return events.fingerprints(session)

    async def stream():
        previous: dict = {}
        since_beat = 0.0
        # An immediate hello lets the browser show "live" without waiting a tick.
        yield f"data: {json.dumps({'topics': [], 'hello': True})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                current = await asyncio.to_thread(_read)
                changed = events.changed_topics(previous, current)
                previous = current
                if changed:
                    since_beat = 0.0
                    yield f"data: {json.dumps({'topics': changed})}\n\n"
            except Exception as exc:  # a bad poll must not kill the stream
                logging.getLogger("bankai.events").warning("event poll failed: %s", exc)
            since_beat += EVENT_POLL_SECONDS
            if since_beat >= EVENT_HEARTBEAT_SECONDS:
                since_beat = 0.0
                yield ": ping\n\n"  # keeps proxies from closing an idle stream
            await asyncio.sleep(EVENT_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Public compliance pages (referenced by Twilio A2P / toll-free verification).
@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return (STATIC_DIR / "privacy.html").read_text()


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return (STATIC_DIR / "terms.html").read_text()


@app.get("/optin", response_class=HTMLResponse)
def optin():
    return (STATIC_DIR / "optin.html").read_text()


@app.get("/api/overview")
def overview(_: str = Depends(require_auth)):
    with session_scope() as session:
        today = date.today()
        last_sync = session.execute(
            select(SyncLog).order_by(SyncLog.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        return {
            "net_worth": net_worth(session),
            # What is owed and when, for accounts whose statements carry terms —
            # a balance alone cannot answer "can we cover the cards this month".
            "account_terms": accounts_terms.terms_by_account(session),
            "history": net_worth_history(session, months=6),
            "this_month": spending_summary(session, today.replace(day=1), today + timedelta(days=1)),
            "upcoming_bills": upcoming_bills(session, days=21),
            "last_sync": (
                {
                    "at": last_sync.started_at.isoformat(),
                    "status": last_sync.status,
                    "detail": last_sync.detail,
                }
                if last_sync
                else None
            ),
            "simplefin_configured": bool(config.SIMPLEFIN_ACCESS_URLS),
            "members": list(sms.household_phones().keys()) or ["Ford", "Spouse"],
        }


@app.get("/api/transactions")
def transactions(limit: int = 100, _: str = Depends(require_auth)):
    with session_scope() as session:
        rows = (
            session.execute(
                select(Transaction).order_by(Transaction.posted.desc()).limit(min(limit, 500))
            )
            .scalars()
            .all()
        )
        return [
            {
                "posted": t.posted.isoformat(),
                "amount": t.amount,
                "description": t.description,
                "category": t.category,
                "account_id": t.account_id,
            }
            for t in rows
        ]


@app.post("/api/import/csv")
async def import_csv_endpoint(
    file: UploadFile = File(...),
    account_name: str = Form(...),
    kind: str = Form("checking"),
    owner: str = Form("joint"),
    _: str = Depends(require_auth),
):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    filename = (file.filename or "").lower()
    is_ofx = filename.endswith((".ofx", ".qfx")) or "<OFX" in text[:2000].upper()
    try:
        with session_scope() as session:
            if is_ofx:
                result = import_ofx(
                    session, text=text, account_name=account_name, kind=kind, owner=owner
                )
            else:
                result = import_csv(
                    session, text=text, account_name=account_name, kind=kind, owner=owner
                )
            return {"added": result.added, "skipped": result.skipped, "format": "ofx" if is_ofx else "csv"}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/sync")
def sync_now(_: str = Depends(require_auth)):
    return simplefin.sync()


@app.post("/api/rules/run")
def run_rules(_: str = Depends(require_auth)):
    return run_rules_once()


@app.get("/api/rules")
def list_rules(_: str = Depends(require_auth)):
    with session_scope() as session:
        rules = session.execute(select(Rule).order_by(Rule.created_at)).scalars().all()
        recent = session.execute(
            select(RuleFiring).order_by(RuleFiring.fired_at.desc()).limit(20)
        ).scalars().all()
        return {
            "kinds": RULE_KINDS,
            "rules": [
                {
                    "id": r.id,
                    "name": r.name,
                    "kind": r.kind,
                    "params": r.params,
                    "message": r.message,
                    "enabled": r.enabled,
                    "created_by": r.created_by,
                }
                for r in rules
            ],
            "recent_firings": [
                {
                    "fired_at": f.fired_at.isoformat(),
                    "subject": f.subject,
                    "delivered": f.delivered,
                }
                for f in recent
            ],
        }


@app.post("/api/rules")
def create_rule(body: RuleBody, _: str = Depends(require_auth)):
    if body.kind not in RULE_KINDS:
        raise HTTPException(400, f"kind must be one of {RULE_KINDS}")
    with session_scope() as session:
        rule = Rule(name=body.name, kind=body.kind, params=body.params, message=body.message)
        session.add(rule)
        session.flush()
        return {"id": rule.id}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        rule = session.get(Rule, rule_id)
        if not rule:
            raise HTTPException(404, "rule not found")
        rule.enabled = False
        return {"ok": True}


@app.post("/api/chat")
def chat_endpoint(body: ChatBody, _: str = Depends(require_auth)):
    try:
        with session_scope() as session:
            reply = sms_thread.handle_web(session, body.speaker, body.message)
    except Exception as exc:
        logging.getLogger("bankai.chat").exception("chat failed")
        raise HTTPException(502, f"Chat failed: {exc}")
    return {"reply": reply}


@app.get("/api/chat/history")
def chat_history(limit: int = 60, _: str = Depends(require_auth)):
    with session_scope() as session:
        rows = (
            session.execute(
                select(ChatMessage)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(min(limit, 200))
            )
            .scalars()
            .all()
        )
        return [
            {
                "role": m.role,
                "speaker": m.speaker,
                "content": m.content,
                "channel": m.channel,
                "at": m.created_at.isoformat(),
            }
            for m in reversed(rows)
        ]


@app.post("/api/accounts")
def upsert_manual_account(body: ManualAccountBody, _: str = Depends(require_auth)):
    """Track a manual asset or liability (home, mortgage, vehicle, loan). Upserts
    by name, so posting again with the same name updates the balance — and each
    update snapshots, so net-worth history reflects it."""
    kind = body.kind.strip().lower()
    if kind not in MANUAL_KINDS:
        raise HTTPException(400, f"kind must be one of {MANUAL_KINDS}")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    with session_scope() as session:
        account = upsert_account(
            session,
            source="manual",
            name=name,
            kind=kind,
            balance=normalize_manual_balance(kind, body.balance),
        )
        account.owner = body.owner
        return {
            "id": account.id,
            "name": account.name,
            "kind": account.kind,
            "balance": account.balance,
        }


@app.delete("/api/accounts/{account_id}")
def delete_manual_account(account_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        account = session.get(Account, account_id)
        if not account:
            raise HTTPException(404, "account not found")
        if account.source != "manual":
            raise HTTPException(400, "only manually tracked accounts can be removed here")
        has_txns = session.execute(
            select(Transaction.id).where(Transaction.account_id == account.id).limit(1)
        ).scalar_one_or_none()
        if has_txns:
            raise HTTPException(400, "account has transactions; cannot remove")
        for snap in session.execute(
            select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)
        ).scalars():
            session.delete(snap)
        session.delete(account)
        return {"ok": True}


def _property_payload(p: Property) -> dict:
    latest = max(p.valuations, key=lambda v: v.created_at, default=None)
    comps = sorted(
        p.comps, key=lambda c: (c.sale_date or date.min), reverse=True
    )
    return {
        "id": p.id,
        "account_id": p.account_id,
        "account_name": p.account.name,
        "street": p.street, "city": p.city, "state": p.state, "zip_code": p.zip_code,
        "sqft": p.sqft, "beds": p.beds, "baths": p.baths, "year_built": p.year_built,
        "auto_update": p.auto_update,
        "current_value": p.account.balance,
        "estimate": realestate.estimate_from_comps(p),
        "latest_valuation": (
            {"value": latest.value, "method": latest.method, "applied": latest.applied,
             "at": latest.created_at.isoformat()}
            if latest else None
        ),
        "comps": [
            {"id": c.id, "address": c.address, "price": c.price, "status": c.status,
             "sale_date": c.sale_date.isoformat() if c.sale_date else None,
             "sqft": c.sqft, "distance_miles": c.distance_miles, "source": c.source}
            for c in comps
        ],
    }


@app.get("/api/properties")
def list_properties(_: str = Depends(require_auth)):
    with session_scope() as session:
        props = session.execute(select(Property)).scalars().all()
        return {
            "rentcast_configured": bool(config.RENTCAST_API_KEY),
            "properties": [_property_payload(p) for p in props],
        }


@app.post("/api/properties")
def upsert_property(body: PropertyBody, _: str = Depends(require_auth)):
    """Attach comps tracking to a manual property account (upserts by account)."""
    with session_scope() as session:
        account = session.get(Account, body.account_id)
        if not account:
            raise HTTPException(404, "account not found")
        if account.source != "manual" or account.kind != "property":
            raise HTTPException(400, "tracking attaches to a manual property account")
        prop = session.execute(
            select(Property).where(Property.account_id == account.id)
        ).scalar_one_or_none()
        if prop is None:
            prop = Property(account_id=account.id, street="", city="", state="")
            session.add(prop)
        prop.street = body.street.strip()
        prop.city = body.city.strip()
        prop.state = body.state.strip()
        prop.zip_code = body.zip_code.strip()
        prop.sqft = body.sqft
        prop.beds = body.beds
        prop.baths = body.baths
        prop.year_built = body.year_built
        prop.auto_update = body.auto_update
        session.flush()
        return _property_payload(prop)


@app.post("/api/properties/{prop_id}/refresh")
def refresh_property_endpoint(prop_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        prop = session.get(Property, prop_id)
        if not prop:
            raise HTTPException(404, "property not found")
        result = realestate.refresh_property(session, prop)
        result["property"] = _property_payload(prop)
        return result


@app.post("/api/properties/{prop_id}/comps")
def add_comp(prop_id: str, body: CompBody, _: str = Depends(require_auth)):
    with session_scope() as session:
        prop = session.get(Property, prop_id)
        if not prop:
            raise HTTPException(404, "property not found")
        sale_date = None
        if body.sale_date:
            try:
                sale_date = date.fromisoformat(body.sale_date)
            except ValueError:
                raise HTTPException(400, "sale_date must be an ISO date")
        comp, created = realestate.upsert_comp(
            session, prop, source="manual", address=body.address, price=body.price,
            status=body.status, sale_date=sale_date, sqft=body.sqft, beds=body.beds,
            baths=body.baths, distance_miles=body.distance_miles,
        )
        return {"id": comp.id, "created": created,
                "estimate": realestate.estimate_from_comps(prop)}


@app.delete("/api/comps/{comp_id}")
def delete_comp(comp_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        comp = session.get(Comp, comp_id)
        if not comp:
            raise HTTPException(404, "comp not found")
        session.delete(comp)
        return {"ok": True}


@app.get("/api/goals")
def list_goals(status: str = "active", _: str = Depends(require_auth)):
    """Household goals with computed progress. status=all for every goal."""
    with session_scope() as session:
        try:
            return {
                "goals": goals_lib.list_goals_with_progress(
                    session, status=None if status == "all" else status
                )
            }
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/api/goals")
def create_goal(body: GoalBody, _: str = Depends(require_auth)):
    with session_scope() as session:
        try:
            goal = goals_lib.create_goal(
                session,
                name=body.name,
                target_amount=body.target_amount,
                category=body.category,
                target_date=date.fromisoformat(body.target_date) if body.target_date else None,
                linked_account_id=body.linked_account_id,
                starting_amount=body.starting_amount,
                note=body.note,
            )
            return goals_lib.goal_progress(session, goal)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.get("/api/watchpoints")
def list_watchpoints(status: str = "", _: str = Depends(require_auth)):
    """Flags the copilot planted for its future self. status=armed|fired|cancelled."""
    with session_scope() as session:
        try:
            rows = watchpoints_lib.list_watchpoints(session, status=status or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {
            "watchpoints": [
                {
                    "id": w.id,
                    "title": w.title,
                    "note": w.note,
                    "kind": w.kind,
                    "status": w.status,
                    "waits_for": watchpoints_lib.describe_condition(w, session),
                    "created_by": w.created_by,
                    "created_at": w.created_at.isoformat(),
                    "fired_at": w.fired_at.isoformat() if w.fired_at else None,
                }
                for w in rows
            ]
        }


MAX_DOCUMENT_BYTES = 15 * 1024 * 1024


@app.post("/api/email/harvest")
def email_harvest_now(_: str = Depends(require_auth)):
    if not email_harvest.configured():
        raise HTTPException(
            400, "email is not connected — set GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env"
        )
    with session_scope() as session:
        return email_harvest.harvest(session)


@app.get("/api/actions")
def list_agent_actions(_: str = Depends(require_auth)):
    with session_scope() as session:
        actions = session.execute(
            select(AgentAction).order_by(AgentAction.proposed_at.desc()).limit(50)
        ).scalars().all()
        return [
            {
                "id": a.id, "kind": a.kind, "title": a.title, "rationale": a.rationale,
                "to_email": a.to_email, "subject": a.subject, "body": a.body,
                "status": a.status, "result": a.result,
                "proposed_at": a.proposed_at.isoformat(),
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
            }
            for a in actions
        ]


@app.post("/api/actions/{action_id}/execute")
def execute_agent_action(action_id: str, _: str = Depends(require_auth)):
    """The human approval gate: this click IS the authorization."""
    with session_scope() as session:
        action = session.get(AgentAction, action_id)
        if not action:
            raise HTTPException(404, "action not found")
        if action.status != "proposed":
            raise HTTPException(400, f"action is already {action.status}")
        try:
            if action.kind == "email_support":
                receipt = email_harvest.send_email(action.to_email, action.subject, action.body)
            elif action.kind == "code_change":
                # Ford's decision (2026-08-11): approving a code change dispatches
                # an agent that implements it. The click stays the authorization —
                # only a human behind APP_PASSWORD reaches this line, and the
                # copilot cannot approve its own proposals. The build itself runs
                # against the worktree behind scope/test/deploy gates in
                # builder.py; this endpoint only starts it and returns.
                if not config.BUILDER_ENABLED:
                    receipt = "accepted for implementation — automatic builder is off"
                else:
                    from . import builder

                    builder.spawn(action_id)
                    # Left `proposed` on purpose: the builder writes the real
                    # outcome (executed / failed) when it finishes, so the
                    # dashboard never shows "done" for work still in flight.
                    return {
                        "status": "building",
                        "result": (
                            f"an {config.BUILDER_MODEL} agent is implementing this now — "
                            "it will report back in the thread when the tests and "
                            "deploy finish"
                        ),
                    }
            else:
                raise RuntimeError(f"no executor for kind {action.kind!r}")
            action.status = "executed"
            action.result = receipt
        except Exception as exc:
            action.status = "failed"
            action.result = str(exc)[:1000]
        action.executed_at = datetime.utcnow()
        session.flush()
        return {"status": action.status, "result": action.result}


@app.post("/api/actions/{action_id}/decline")
def decline_agent_action(action_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        action = session.get(AgentAction, action_id)
        if not action:
            raise HTTPException(404, "action not found")
        if action.status != "proposed":
            raise HTTPException(400, f"action is already {action.status}")
        action.status = "declined"
        action.result = "declined from the dashboard"
        return {"status": "declined"}


@app.get("/api/documents")
def list_documents(_: str = Depends(require_auth)):
    with session_scope() as session:
        docs = session.execute(
            select(Document).order_by(Document.added_at.desc())
        ).scalars().all()
        return {
            "categories": vault.CATEGORIES,
            "email_configured": email_harvest.configured(),
            "documents": [
                {
                    "id": d.id,
                    "title": d.title,
                    "category": d.category,
                    "filename": d.filename,
                    "size_bytes": d.size_bytes,
                    "chars": len(d.content_text),
                    "summary": d.summary,
                    "added_at": d.added_at.isoformat(),
                }
                for d in docs
            ],
        }


@app.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(""),
    category: str = Form("other"),
    _: str = Depends(require_auth),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "File exceeds the 15 MB limit")
    with session_scope() as session:
        doc, created = vault.add_document(
            session,
            filename=file.filename or "upload",
            data=data,
            title=title.strip() or None,
            category=category,
        )
        return {
            "id": doc.id,
            "created": created,
            "title": doc.title,
            "category": doc.category,
            "chars": len(doc.content_text),
        }


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if not doc:
            raise HTTPException(404, "document not found")
        vault.delete_document(session, doc)
        return {"ok": True}


@app.get("/api/memories")
def list_memories(_: str = Depends(require_auth)):
    with session_scope() as session:
        notes = session.execute(
            select(MemoryNote).order_by(MemoryNote.updated_at.desc())
        ).scalars().all()
        return [
            {
                "id": n.id,
                "title": n.title,
                "content": n.content,
                "updated_at": n.updated_at.isoformat(),
            }
            for n in notes
        ]


@app.delete("/api/memories/{note_id}")
def delete_memory(note_id: str, _: str = Depends(require_auth)):
    with session_scope() as session:
        note = session.get(MemoryNote, note_id)
        if not note:
            raise HTTPException(404, "memory not found")
        session.delete(note)
        return {"ok": True}


_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _webhook_url(request: Request) -> str:
    if config.SMS_PUBLIC_URL:
        return config.SMS_PUBLIC_URL
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}{request.url.path}"


@app.post("/api/sms/webhook")
async def sms_webhook(request: Request):
    """Twilio inbound SMS. Signature-validated; only household numbers get replies."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    if not sms.validate_signature(_webhook_url(request), params, signature):
        raise HTTPException(403, "invalid Twilio signature")
    sender = sms.identify_sender(params.get("From", ""))
    if sender:
        with session_scope() as session:
            sms_thread.handle_inbound(session, sender, params.get("Body", ""))
    # Unknown numbers are ignored silently. Replies go out via the REST API,
    # so the TwiML response is always empty.
    return Response(content=_EMPTY_TWIML, media_type="application/xml")
