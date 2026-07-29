"""FastAPI app: shared-password auth, dashboard, JSON API, chat endpoint."""
from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from . import config
from .connectors import simplefin
from .connectors.csv_import import import_csv
from .connectors.ofx_import import import_ofx
from .db import init_db, session_scope
from .messaging import sms
from .messaging import thread as sms_thread
from .intelligence.insights import net_worth, net_worth_history, spending_summary, upcoming_bills
from .models import ChatMessage, MemoryNote, Rule, RuleFiring, SyncLog, Transaction
from .rules.engine import RULE_KINDS
from .scheduler import run_rules_once, start_background_tasks

logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "bankai_session"


def _session_token() -> str:
    return hmac.new(
        config.SESSION_SECRET.encode(), b"bankai-authenticated", hashlib.sha256
    ).hexdigest()


def require_auth(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME, "")
    if not config.APP_PASSWORD:
        raise HTTPException(500, "APP_PASSWORD is not configured")
    if not hmac.compare_digest(token, _session_token()):
        raise HTTPException(401, "Not authenticated")
    return token


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = start_background_tasks()
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="BankAI", lifespan=lifespan)


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


@app.post("/api/login")
def login(body: LoginBody):
    if not config.APP_PASSWORD or not hmac.compare_digest(body.password, config.APP_PASSWORD):
        raise HTTPException(401, "Wrong password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME, _session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90
    )
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/health")
def health():
    return {"ok": True}


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
            "simplefin_configured": bool(config.SIMPLEFIN_ACCESS_URL),
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
