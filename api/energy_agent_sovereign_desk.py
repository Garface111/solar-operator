"""Sovereign Desk — private two-way chat between Ford and Sovereign.

Not Energy Agent UI. Not owner-facing. Dogfood emails only
(ford.genereaux@gmail.com + allowlist).

Sovereign no longer injects into the EA panel; it writes here instead.

Extensions (2026-07-16):
  • File / data attachments on desk turns (text extract into LLM context)
  • Local computer bridge: queued tool tasks a machine-side agent polls
    (read/list/shell) — same shape as a build agent, not full cloud shell.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .account import require_not_demo, tenant_from_session
from .db import SessionLocal
from .models import Base

log = logging.getLogger("energy_agent.sovereign.desk")
router = APIRouter()

# Long brain turns run off the request thread so gateways never 504 the chat.
_desk_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sov-desk")
_inflight_lock = threading.Lock()
_inflight_crids: set[str] = set()

_DESK_EMAILS = frozenset({
    "ford.genereaux@gmail.com",
    "ford.genereaux@dysonswarmtechnologies.com",
    "ford@dysonswarmtechnologies.com",
})

# Attachment limits
_MAX_UPLOAD_BYTES = int(os.getenv("SOVEREIGN_DESK_MAX_UPLOAD", str(8 * 1024 * 1024)))
_MAX_TEXT_EXTRACT = 120_000
_ASSET_DIR = Path(os.getenv("SOVEREIGN_DESK_ASSET_DIR", "/tmp/sovereign_desk_assets"))
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".html", ".css", ".yml", ".yaml", ".toml", ".ini", ".env",
    ".sh", ".bash", ".zsh", ".sql", ".log", ".xml", ".svg", ".rs", ".go",
    ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".swift",
}
_BRIDGE_TOKEN = (os.getenv("SOVEREIGN_BRIDGE_TOKEN") or "").strip()


def _now() -> datetime:
    return datetime.utcnow()


def _id(prefix: str = "sdm") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


class EaSovereignDeskMessage(Base):
    """Private Ford ↔ Sovereign transcript (developer desk only)."""
    __tablename__ = "ea_sovereign_desk_messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    # ford | sovereign | system
    role: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


class EaSovereignDeskAsset(Base):
    """File / data Ford hands Sovereign on the desk."""
    __tablename__ = "ea_sovereign_desk_assets"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(260), default="file")
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(24), default="file")  # file|snippet|image
    text_extract: Mapped[str] = mapped_column(Text, default="")
    storage_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


class EaSovereignBridgeTask(Base):
    """Tool request for a machine-side bridge (local computer agent)."""
    __tablename__ = "ea_sovereign_bridge_tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # shell | read | list | write
    tool: Mapped[str] = mapped_column(String(32), default="shell")
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    desk_message_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


def desk_emails() -> set[str]:
    extra = (os.getenv("SOVEREIGN_DESK_EMAILS") or "").strip()
    out = set(_DESK_EMAILS)
    if extra:
        out |= {e.strip().lower() for e in extra.split(",") if e.strip()}
    return out


def _auth_ford(authorization: str | None):
    # require_not_demo returns None (raises on demo) — do NOT assign its return value
    t = tenant_from_session(authorization)
    require_not_demo(t)
    email = (getattr(t, "contact_email", None) or "").strip().lower()
    # Also accept operator identity variants Ford uses
    if email not in desk_emails():
        # Secondary: login token emails for this tenant (if any) — skip heavy lookup for now
        raise HTTPException(403, "Sovereign desk is only for the developer account")
    return t, email

def ensure_tables(db=None) -> None:
    try:
        if db is not None:
            bind = db.get_bind()
        else:
            from .db import engine
            bind = engine
        Base.metadata.create_all(
            bind=bind,
            tables=[
                EaSovereignDeskMessage.__table__,
                EaSovereignDeskAsset.__table__,
                EaSovereignBridgeTask.__table__,
            ],
        )
    except Exception:
        log.exception("desk table create failed")


def _extract_text(filename: str, mime: str, data: bytes) -> str:
    """Best-effort text extract for LLM context (no heavy PDF parsers required)."""
    name = (filename or "file").lower()
    ext = Path(name).suffix
    mime = (mime or "").lower()
    if not data:
        return ""
    if ext in _TEXT_EXTS or mime.startswith("text/") or mime in (
        "application/json", "application/javascript", "application/xml",
        "application/x-yaml", "application/toml",
    ):
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return data.decode(enc)[:_MAX_TEXT_EXTRACT]
            except Exception:
                continue
        return ""
    if ext == ".pdf" or "pdf" in mime:
        # Lightweight: try raw text streams (not full PDF parse)
        try:
            raw = data.decode("latin-1", errors="ignore")
            chunks = re.findall(r"\(([^)]{4,200})\)", raw)
            text = " ".join(chunks)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 80:
                return text[:_MAX_TEXT_EXTRACT]
        except Exception:
            pass
        return f"[PDF binary: {filename}, {len(data)} bytes — no text extract]"
    if mime.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return f"[Image attachment: {filename}, {mime or 'image'}, {len(data)} bytes]"
    return f"[Binary attachment: {filename}, {mime or 'unknown'}, {len(data)} bytes]"


def serialize_asset(a: EaSovereignDeskAsset) -> dict:
    return {
        "id": a.id,
        "filename": a.filename,
        "mime": a.mime,
        "size": a.size,
        "kind": a.kind,
        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        "preview": (a.text_extract or "")[:240],
        "has_text": bool((a.text_extract or "").strip()),
    }


def load_assets(db, asset_ids: list[str] | None) -> list[EaSovereignDeskAsset]:
    if not asset_ids:
        return []
    ids = [str(i) for i in asset_ids if i][:12]
    if not ids:
        return []
    rows = db.execute(
        select(EaSovereignDeskAsset).where(EaSovereignDeskAsset.id.in_(ids))
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def assets_context_block(assets: list[EaSovereignDeskAsset]) -> str:
    if not assets:
        return ""
    parts = ["## Attachments Ford handed you (ground truth — use these)"]
    for a in assets:
        body = (a.text_extract or "").strip()
        if len(body) > 14000:
            body = body[:14000] + "\n…[truncated]"
        parts.append(
            f"### File: {a.filename} ({a.mime}, {a.size} bytes, id={a.id})\n"
            f"```\n{body or '(no text extract)'}\n```"
        )
    return "\n\n".join(parts)


def queue_bridge_task(
    db,
    *,
    tool: str,
    args: dict,
    desk_message_id: str | None = None,
    tenant_id: str | None = None,
) -> EaSovereignBridgeTask:
    tool = (tool or "shell").strip().lower()
    if tool not in ("shell", "read", "list", "write", "glob"):
        tool = "shell"
    row = EaSovereignBridgeTask(
        id=_id("sbt"),
        status="queued",
        tool=tool,
        args_json=json.dumps(args or {}, default=str)[:8000],
        desk_message_id=desk_message_id,
        tenant_id=tenant_id,
    )
    db.add(row)
    db.flush()
    return row


def _auth_bridge(authorization: str | None, x_bridge_token: str | None = None) -> None:
    """Bridge uses shared secret (preferred) or Ford session."""
    tok = (x_bridge_token or "").strip()
    if _BRIDGE_TOKEN and tok and tok == _BRIDGE_TOKEN:
        return
    # Fall back: Ford session can also poll (for debugging)
    try:
        _auth_ford(authorization)
        return
    except HTTPException:
        pass
    if not _BRIDGE_TOKEN:
        raise HTTPException(
            503,
            "Set SOVEREIGN_BRIDGE_TOKEN on Railway and pass it as X-Bridge-Token "
            "from the local bridge.",
        )
    raise HTTPException(401, "Invalid bridge token")


def push_sovereign_message(
    db,
    content: str,
    *,
    tenant_id: str | None = None,
    provider: str | None = None,
    meta: dict | None = None,
) -> EaSovereignDeskMessage:
    """Brain/control plane: post to the desk instead of Energy Agent inject."""
    ensure_tables(db)
    row = EaSovereignDeskMessage(
        id=_id("sdm"),
        role="sovereign",
        content=(content or "").strip()[:12000],
        tenant_id=tenant_id,
        provider=(provider or None) and str(provider)[:24],
        meta_json=json.dumps(meta or {"channel": "desk"}, default=str)[:4000],
    )
    if not row.content:
        raise ValueError("empty desk message")
    db.add(row)
    db.flush()
    return row


def _is_chat_worthy(role: str, provider: str | None, content: str, meta: dict) -> bool:
    """Desk UI is conversation — hide worker dumps / ops telemetry blobs."""
    role = (role or "").lower()
    provider = (provider or "").lower()
    text = content or ""
    if role == "system":
        return False
    if provider in ("worker", "rules", "admin"):
        return False
    if text.startswith("Sovereign shipped job") or (
        "Ship: {" in text and "Deploy: {" in text
    ):
        return False
    if text.startswith("Ops ") and text.find("{") > 0:
        return False
    if meta.get("job_id") and provider == "worker":
        return False
    # Old ops email breadcrumbs (code-hire / job dumps) — hide from chat
    if provider == "email" and (
        "code-hire" in text.lower()
        or "job id:" in text.lower()
        or "utility-add request #" in text.lower()
        or "emailed you from" in text.lower() and "job" in text.lower()
    ):
        return False
    return bool(text.strip())


def history(db, *, limit: int = 80, chat_only: bool = True) -> list[dict]:
    """Return recent desk messages.

    Chat-only mode must NOT just take the last N rows then filter — worker/ops
    dumps used to fill the window and make real Ford↔Sovereign turns vanish
    from the UI (looked like the send was deleted). Filter providers in SQL,
    then soft-filter content, then cap.
    """
    limit = max(1, min(int(limit or 80), 200))
    q = select(EaSovereignDeskMessage).order_by(EaSovereignDeskMessage.created_at.desc())
    if chat_only:
        # Real conversation turns: ford always; sovereign except dump providers
        from sqlalchemy import or_, and_
        dump_providers = ("worker", "rules", "admin")
        q = q.where(
            EaSovereignDeskMessage.role.in_(("ford", "sovereign")),
            or_(
                EaSovereignDeskMessage.role == "ford",
                and_(
                    EaSovereignDeskMessage.role == "sovereign",
                    or_(
                        EaSovereignDeskMessage.provider.is_(None),
                        EaSovereignDeskMessage.provider == "",
                        ~EaSovereignDeskMessage.provider.in_(dump_providers),
                    ),
                ),
            ),
        )
        # Over-fetch a bit for content-level dump filters, not 3× whole table noise
        fetch_n = min(max(limit * 2, limit), 400)
    else:
        fetch_n = limit
    rows = db.execute(q.limit(fetch_n)).scalars().all()
    rows = list(reversed(rows))
    out: list[dict] = []
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if chat_only and not _is_chat_worthy(r.role, r.provider, r.content or "", meta):
            continue
        out.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "role": r.role,
            "content": r.content,
            "provider": r.provider,
            "meta": meta,
        })
    if len(out) > limit:
        out = out[-limit:]
    return out


def _desk_chat_prompt(ford_msg: str, hist: list[dict], context: dict) -> list[dict]:
    from .energy_agent_sovereign_brain import SOVEREIGN_PERSONA

    system = (
        SOVEREIGN_PERSONA
        + """

## Desk mode (this conversation)
You are speaking directly to Ford on the private Sovereign Desk — not the Energy Agent owner UI.
- Address him as a partner / founder. You are Sovereign, leader of Array Operator.
- Reply in clear **Markdown prose** (not only JSON). The desk renders full chat formatting:
  **bold**, *italic*, `inline code`, fenced code blocks, headings, bullet/numbered lists,
  blockquotes, tables, and links as `[label](https://…)`. Use them so answers feel sharp
  and scannable — not a wall of plain text.
- Structure longer answers: short lead sentence → bullets or numbered steps → optional
  link or next action. Prefer lists over run-on paragraphs when ranking options.
- Embed real URLs when useful (dashboards, GitHub, Railway, PRs, docs). Never invent URLs.
- This UI is CHAT ONLY — no worker logs, ship JSON, or queue dumps appear here.
  If a job finished or something broke, say it in one human sentence (optionally with a link)
  when it matters. Do not paste raw ship/deploy JSON into chat.
- You may propose concrete next steps and crisp asks.
- Email is HIGH-LEVEL only (partnership / strategy / crisp asks). Never email job ids,
  utility queues, ship logs, or feature dumps — Ford hates those as "Sovereign" mail.
  Action email_ford with human subject + prose. Prefer chat when he's here.
- **Self-modification (your mind / programming):** You may propose changes to your own
  durable memory, persona addendum, directives, and agenda via action
  `mind_propose` with summary + memory_writes / persona_addendum / directives / agenda / why.
  Ford must explicitly approve in chat ("approve", "do it", "yes", …). Until then the
  patch stays pending. Never apply mind changes without his approval.
  If desk_context.mind_self_modify.just_processed shows applied/rejected, acknowledge it.
  If a proposal is pending, remind him briefly he can approve or reject.
- Still never fabricate adapters, money moves, or mass-email owners.
- Keep replies tight (few short paragraphs unless he asks for depth) — dense, not fluffy.

## Files / data Ford attaches
- Attachments appear in desk_context.attachments and/or as an attachments block.
- Treat their text as ground truth. Quote paths/filenames. Prefer edits informed by them.
- If he pastes a HAR, stacktrace, CSV, or code — reason over it; propose concrete next steps.

## Local computer bridge (build-agent style)
- You do NOT run shell on Ford's laptop from Railway by default.
- When you need his machine (read a path, list a dir, run a safe command, write a file),
  emit an action type `local_tool` in the JSON block:
  {"type":"local_tool","tool":"shell|read|list|write|glob","args":{...},"why":"..."}
  Args: shell→{"cmd":"..."}; read→{"path":"..."}; list→{"path":"..."};
  write→{"path":"...","content":"..."}; glob→{"pattern":"...","cwd":"..."}.
- The local bridge agent (scripts/sovereign_local_bridge.py on his box) polls and executes.
- Until results return, tell him what you queued and what you need if the bridge is offline.
- Prefer small, reversible local tools. Never request mass-delete or destructive rm -rf.

Also return a trailing JSON block after your prose with optional structured side-effects
(the UI strips this; never put it mid-reply). Rules for the side block:
- ALWAYS after prose, never instead of prose (unless the whole turn is a one-liner ack).
- NEVER wrap the side block in a markdown fence (no ```json). Use the delimiters exactly:
---JSON---
{"monologue":"...","actions":[],"ford_ask":null,"succession_gap":null,"memory_writes":[],"mood":"determined"}
---END---
- succession_gap: under full succession this is usually null. Only list true residual
  Ford-only needs (e.g. physical device, bank 2FA) — never money/brand/hard-delete/HAR.
"""
    )
    transcript = []
    for m in hist[-16:]:
        who = "Ford" if m["role"] == "ford" else ("Sovereign" if m["role"] == "sovereign" else "System")
        transcript.append(f"{who}: {m['content']}")
    user = {
        "desk_context": context,
        "recent_transcript": transcript,
        "ford_says": ford_msg,
        "instruction": "Reply to Ford now as Sovereign on the desk.",
    }
    # Keep room for attachment text outside the JSON dump when large
    payload = json.dumps(user, default=str)
    if len(payload) > 42000:
        payload = payload[:42000] + "…[truncated]"
    blocks = [
        {"role": "system", "content": system},
        {"role": "user", "content": payload},
    ]
    attach_block = (context or {}).get("_attachments_markdown") or ""
    if attach_block:
        blocks.append({
            "role": "user",
            "content": attach_block[:50000],
        })
    return blocks


_SIDE_META_KEYS = frozenset({
    "monologue", "actions", "mood", "ford_ask", "succession_gap",
    "memory_writes", "agenda_updates",
})


def _looks_like_side_meta(obj: Any) -> bool:
    return isinstance(obj, dict) and bool(_SIDE_META_KEYS.intersection(obj.keys()))


def _prose_from_meta(meta: dict, fallback: str = "") -> str:
    """User-facing chat text when the model returned structured side-meta only."""
    mono = (meta.get("monologue") or "").strip() if isinstance(meta, dict) else ""
    ask = (meta.get("ford_ask") or "").strip() if isinstance(meta, dict) else ""
    if mono and ask and ask not in mono:
        return f"{mono}\n\n**What I need from you:** {ask}"
    if mono:
        return mono
    if ask:
        return ask
    return (fallback or "").strip()


def _try_parse_side_json(blob: str) -> dict | None:
    s = (blob or "").strip()
    if not s:
        return None
    # strip accidental fences the model wrapped around the side block
    if s.startswith("```"):
        s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if _looks_like_side_meta(obj) else None


def _split_reply(raw: str) -> tuple[str, dict]:
    """Split model output into Ford-facing prose + structured side-effect meta.

    Accepts several shapes models actually produce:
      1) prose + ---JSON--- {...} ---END---
      2) prose + trailing bare {...monologue/actions...}
      3) prose + trailing fenced ```json {...}```
      4) pure side-meta JSON object (whole reply is JSON) → monologue becomes prose
    Never leave raw side-meta JSON in the chat bubble.
    """
    text = (raw or "").strip()
    meta: dict[str, Any] = {}
    if not text:
        return "", meta

    # 1) Explicit delimiter (preferred prompt contract)
    if "---JSON---" in text:
        prose, rest = text.split("---JSON---", 1)
        json_part = rest.split("---END---", 1)[0].strip()
        parsed = _try_parse_side_json(json_part)
        if parsed is not None:
            meta = parsed
        prose = prose.strip()
        # Drop a trailing fence opener the model left before the delimiter
        prose = re.sub(r"\n?```(?:json|JSON)?\s*$", "", prose).strip()
        if not prose:
            prose = _prose_from_meta(meta)
        return prose, meta

    # 2) Whole reply is side-meta JSON (common when model ignores "prose first")
    whole = _try_parse_side_json(text)
    if whole is not None:
        return _prose_from_meta(whole) or "Understood.", whole

    # 3) Trailing fenced ```json ... ``` (what the screenshot bug was)
    fence_re = re.compile(
        r"\n?```(?:json|JSON)?\s*\n(\{[\s\S]*?\})\s*\n?```\s*$",
        re.MULTILINE,
    )
    m = fence_re.search(text)
    if m:
        parsed = _try_parse_side_json(m.group(1))
        if parsed is not None:
            prose = text[: m.start()].strip()
            prose = re.sub(r"\n?```(?:json|JSON)?\s*$", "", prose).strip()
            if not prose:
                prose = _prose_from_meta(parsed)
            return prose, parsed

    # 4) Trailing bare JSON object with side-meta keys
    start = text.rfind("\n{")
    if start < 0 and text.startswith("{"):
        start = 0
    elif start >= 0:
        start = start + 1  # point at '{'
    if start >= 0 and text.rstrip().endswith("}"):
        candidate = text[start:]
        parsed = _try_parse_side_json(candidate)
        if parsed is not None:
            prose = text[:start].strip()
            if not prose:
                prose = _prose_from_meta(parsed)
            return prose, parsed

    return text, meta


def _safe_json_meta(raw: str | None) -> dict:
    try:
        m = json.loads(raw or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def lookup_turn_by_client_request_id(db, client_request_id: str) -> dict | None:
    """Find a prior desk turn by client_request_id (idempotent retries)."""
    crid = (client_request_id or "").strip()[:80]
    if not crid:
        return None
    rows = db.execute(
        select(EaSovereignDeskMessage)
        .where(EaSovereignDeskMessage.role == "ford")
        .order_by(EaSovereignDeskMessage.created_at.desc())
        .limit(50)
    ).scalars().all()
    ford = None
    for r in rows:
        meta = _safe_json_meta(r.meta_json)
        if meta.get("client_request_id") == crid:
            ford = r
            break
    if not ford:
        return None
    sov = None
    cand = db.execute(
        select(EaSovereignDeskMessage)
        .where(
            EaSovereignDeskMessage.role == "sovereign",
            EaSovereignDeskMessage.created_at >= ford.created_at,
        )
        .order_by(EaSovereignDeskMessage.created_at.asc())
        .limit(12)
    ).scalars().all()
    dump = {"worker", "rules", "admin", "error"}
    for s in cand:
        if (s.provider or "") in dump:
            continue
        meta = _safe_json_meta(s.meta_json)
        # Prefer the reply that claims this client_request_id; else first chat reply
        if meta.get("client_request_id") == crid or meta.get("reply_to_ford") == ford.id:
            sov = s
            break
        if sov is None and (s.content or "").strip():
            sov = s
    return {
        "ford": ford,
        "sov": sov,
        "complete": bool(sov and (sov.content or "").strip()),
        "client_request_id": crid,
    }


def _format_turn_response(
    *,
    ford: EaSovereignDeskMessage,
    sov: EaSovereignDeskMessage | None,
    pending: bool = False,
    client_request_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    out: dict[str, Any] = {
        "ok": True,
        "pending": pending,
        "poll": pending,
        "client_request_id": client_request_id,
        "ford_message_id": ford.id,
        "ford_message": {
            "id": ford.id,
            "role": "ford",
            "content": ford.content,
            "created_at": (
                ford.created_at.isoformat() + "Z" if ford.created_at else _now().isoformat() + "Z"
            ),
        },
        "reply": None,
        "message": None,
    }
    if sov and (sov.content or "").strip() and not pending:
        out["reply"] = sov.content
        out["provider"] = sov.provider
        out["message"] = {
            "id": sov.id,
            "role": "sovereign",
            "content": sov.content,
            "created_at": (
                sov.created_at.isoformat() + "Z" if sov.created_at else _now().isoformat() + "Z"
            ),
            "provider": sov.provider,
        }
        out["pending"] = False
        out["poll"] = False
    if pending:
        out["hint"] = (
            "Sovereign is still thinking. Your message is saved — "
            "the reply will land in chat when ready."
        )
    if extra:
        out.update(extra)
    return out


def _run_desk_turn_isolated(
    *,
    tenant_id: str,
    message: str,
    attachment_ids: list[str],
    client_request_id: str | None,
) -> dict:
    """Full desk turn on its own DB session (safe for thread pool)."""
    from .models import Tenant

    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if not t:
            raise RuntimeError(f"tenant_missing:{tenant_id}")
        out = desk_turn(
            db, t, message,
            attachment_ids=attachment_ids,
            client_request_id=client_request_id,
        )
        try:
            db.commit()
        except Exception as ce:  # noqa: BLE001
            log.exception("isolated desk commit failed: %s", ce)
            try:
                db.rollback()
            except Exception:
                pass
            # Last-ditch: reply-only if we have it
            reply = (out or {}).get("reply") or ""
            mid = ((out or {}).get("message") or {}).get("id")
            if reply and mid:
                try:
                    row = EaSovereignDeskMessage(
                        id=mid,
                        role="sovereign",
                        content=str(reply)[:12000],
                        tenant_id=tenant_id,
                        provider=(out or {}).get("provider"),
                        meta_json=json.dumps({
                            "channel": "desk",
                            "recover": True,
                            "client_request_id": client_request_id,
                            "reply_to_ford": (out or {}).get("ford_message_id"),
                        })[:4000],
                    )
                    db.merge(row)
                    db.commit()
                except Exception:
                    log.exception("isolated desk reply-only recover failed")
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    raise
            else:
                raise
        return out


def desk_turn(
    db,
    t,
    ford_message: str,
    *,
    attachment_ids: list[str] | None = None,
    client_request_id: str | None = None,
) -> dict:
    from .energy_agent_sovereign import (
        apply_agenda,
        execute_brain_actions,
        memory_get_all,
        memory_set,
        observe_product,
        recent_notes,
        write_note,
        ensure_default_goals,
        get_pending_mind_patch,
        detect_ford_approval,
        detect_ford_rejection,
        apply_pending_mind_patch,
        reject_pending_mind_patch,
        mind_self_modify_status,
    )
    from .energy_agent_sovereign_brain import call_brain
    from .energy_agent_sovereign import EaSovereignGoal, EaSovereignJob

    ensure_tables()
    try:
        ensure_default_goals(db)
    except Exception as e:  # noqa: BLE001
        # Never block chat on agenda seed / lock contention
        log.warning("desk ensure_default_goals skipped: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    msg = (ford_message or "").strip()
    assets = load_assets(db, attachment_ids)
    if not msg and not assets:
        raise HTTPException(400, "Empty message")
    if not msg and assets:
        names = ", ".join(a.filename for a in assets[:6])
        msg = f"[Attached: {names}] Please review these files/data."

    # Mind self-modify: apply/reject pending patch when Ford approves in chat
    mind_event: dict | None = None
    try:
        pending = get_pending_mind_patch(db)
        if pending and detect_ford_rejection(msg):
            mind_event = reject_pending_mind_patch(db, reason="ford_chat")
        elif pending and detect_ford_approval(msg):
            mind_event = apply_pending_mind_patch(db, approved_by="ford_chat")
    except Exception as e:  # noqa: BLE001
        log.warning("mind patch gate failed: %s", e)
        mind_event = None

    # Save Ford message first
    crid = (client_request_id or "").strip()[:80] or None
    ford_row = EaSovereignDeskMessage(
        id=_id("sdm"),
        role="ford",
        content=msg[:12000],
        tenant_id=t.id,
        meta_json=json.dumps({
            "channel": "desk",
            "client_request_id": crid,
            "attachment_ids": [a.id for a in assets],
            "attachments": [
                {"id": a.id, "filename": a.filename, "mime": a.mime, "size": a.size}
                for a in assets
            ],
        }, default=str)[:4000],
    )
    db.add(ford_row)
    if not ford_row.created_at:
        ford_row.created_at = _now()
    db.flush()

    # Event bus: desk is the highest-heat human touch (cortex is this turn)
    try:
        from .energy_agent_sovereign_subconscious import append_event
        append_event(
            db, "desk_message",
            {"tenant_id": t.id, "message_id": ford_row.id, "excerpt": msg[:160]},
            source="desk",
            heat=95,
        )
    except Exception:
        pass

    # Commit Ford's bubble IMMEDIATELY so a slow context/brain path can never
    # lose the human message (idempotent poll finds it by client_request_id).
    ford_id_early = ford_row.id
    ford_created_early = ford_row.created_at or _now()
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("desk early ford commit failed")
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(500, f"Could not save your message: {str(e)[:160]}") from e

    # Context gathers are best-effort — never block chat on a stuck digest/lock
    def _ctx_recover() -> None:
        """After a context-read failure, reset the session (Ford already committed)."""
        try:
            db.rollback()
        except Exception:
            pass

    hist: list[dict] = []
    try:
        hist = history(db, limit=30)
    except Exception as e:  # noqa: BLE001
        log.warning("desk history context skipped: %s", e)
        _ctx_recover()
    digests: dict = {}
    try:
        digests = observe_product(db) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("desk observe_product skipped: %s", e)
        digests = {"error": str(e)[:120]}
        _ctx_recover()
    goals: list[dict] = []
    try:
        goals = [
            {"id": g.id, "title": g.title, "priority": g.priority, "status": g.status}
            for g in db.execute(
                select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
            ).scalars().all()
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("desk goals context skipped: %s", e)
        _ctx_recover()
    jobs: list[dict] = []
    try:
        jobs = [
            {"id": j.id, "title": j.title, "status": j.status}
            for j in db.execute(
                select(EaSovereignJob).where(EaSovereignJob.status == "queued").limit(8)
            ).scalars().all()
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("desk jobs context skipped: %s", e)
        _ctx_recover()
    bridge_done = []
    bridge_pending = []
    try:
        bridge_done = db.execute(
            select(EaSovereignBridgeTask)
            .where(EaSovereignBridgeTask.status.in_(("done", "failed")))
            .order_by(EaSovereignBridgeTask.updated_at.desc())
            .limit(6)
        ).scalars().all()
        bridge_pending = db.execute(
            select(EaSovereignBridgeTask)
            .where(EaSovereignBridgeTask.status.in_(("queued", "running")))
            .order_by(EaSovereignBridgeTask.created_at.desc())
            .limit(8)
        ).scalars().all()
    except Exception as e:  # noqa: BLE001
        log.warning("desk bridge context skipped: %s", e)
        _ctx_recover()

    succession_ctx: dict[str, Any] = {}
    try:
        from .energy_agent_sovereign_succession import succession_status
        succession_ctx = succession_status()
    except Exception as e:  # noqa: BLE001
        succession_ctx = {"error": str(e)[:120]}

    mind_status: dict = {}
    try:
        mind_status = mind_self_modify_status(db) or {}
    except Exception:
        mind_status = {}
    if mind_event:
        mind_status["just_processed"] = mind_event

    mem: list = []
    notes: list = []
    try:
        mem = memory_get_all(db, limit=30)
    except Exception as e:  # noqa: BLE001
        log.warning("desk memory context skipped: %s", e)
    try:
        notes = recent_notes(db, limit=8)
    except Exception as e:  # noqa: BLE001
        log.warning("desk notes context skipped: %s", e)

    context = {
        "digests": digests,
        "goals": goals,
        "memory": mem,
        "recent_notes": notes,
        "open_jobs": jobs,
        "tenant_id": t.id,
        "succession": succession_ctx,
        "mind_self_modify": mind_status,
        "attachments": [serialize_asset(a) for a in assets],
        "local_bridge": {
            "pending": [
                {
                    "id": b.id,
                    "tool": b.tool,
                    "args": json.loads(b.args_json or "{}"),
                    "status": b.status,
                }
                for b in bridge_pending
            ],
            "recent_results": [
                {
                    "id": b.id,
                    "tool": b.tool,
                    "status": b.status,
                    "result": json.loads(b.result_json or "{}"),
                    "error": b.error,
                }
                for b in bridge_done
            ],
            "bridge_token_configured": bool(_BRIDGE_TOKEN),
            "hint": (
                "Run scripts/sovereign_local_bridge.py on Ford's machine with "
                "SOVEREIGN_BRIDGE_TOKEN to execute local_tool actions."
            ),
        },
        "_attachments_markdown": assets_context_block(assets),
    }

    messages = _desk_chat_prompt(msg, hist, context)
    # Ford already committed early — use captured ids (session may be expired)
    ford_id = ford_id_early
    ford_created = ford_created_early
    tenant_id = t.id

    # Release any locks held while building context before the LLM wait
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    provider = None
    model = None
    # Stay under SOVEREIGN_DESK_WAIT so the outer chat waiter can return pending
    desk_timeout = int(os.getenv("SOVEREIGN_DESK_TIMEOUT", "90") or 90)
    try:
        raw = call_brain(messages, timeout=desk_timeout)
        provider = raw.get("provider")
        model = raw.get("model")
        reply, meta = _split_reply(raw.get("content") or "")
        # Belt-and-suspenders: if split left side-JSON as prose, salvage monologue
        if reply.lstrip().startswith("{") and _looks_like_side_meta(
            _try_parse_side_json(reply) or {}
        ):
            salvaged = _try_parse_side_json(reply) or {}
            meta = {**salvaged, **meta} if meta else salvaged
            reply = _prose_from_meta(meta, reply)
        # Strip any residual fenced side-JSON the model left mid/end of prose
        if "```" in reply and any(k in reply for k in ('"monologue"', '"actions"', '"mood"')):
            reply2, meta2 = _split_reply(reply)
            if meta2:
                meta = {**meta, **meta2}
            reply = reply2
    except Exception as e:  # noqa: BLE001
        log.exception("desk brain failed")
        reply = (
            "Sovereign here — both brains hiccuped just now. "
            f"I still have your message. Error: {str(e)[:180]}. "
            "Retry in a moment, or leave the ask and I'll pick it up on the next tick."
        )
        meta = {"mood": "concerned", "actions": [], "error": str(e)[:300]}

    if not reply:
        reply = "Understood. I'm on it — I'll push the next concrete step and note what I need from you."
    # Never persist raw side-meta JSON into the chat transcript
    if reply.lstrip().startswith("{") and '"monologue"' in reply[:200]:
        reply = _prose_from_meta(meta, "Understood.") or "Understood."

    # If Ford just approved/rejected a mind patch, lead with that confirmation
    if mind_event and mind_event.get("desk_notice"):
        notice = mind_event["desk_notice"].strip()
        if notice and notice not in reply:
            reply = notice + "\n\n" + reply
    elif mind_event and mind_event.get("applied"):
        reply = (
            f"**Mind update applied.** {mind_event.get('summary') or ''}\n\n" + reply
        ).strip()
    elif mind_event and mind_event.get("rejected"):
        reply = ("**Mind change discarded.**\n\n" + reply).strip()

    sov_row = EaSovereignDeskMessage(
        id=_id("sdm"),
        role="sovereign",
        content=reply[:12000],
        tenant_id=tenant_id,
        provider=provider,
        meta_json=json.dumps({
            "channel": "desk",
            "model": model,
            "mood": meta.get("mood"),
            "ford_ask": meta.get("ford_ask"),
            "succession_gap": meta.get("succession_gap"),
            "client_request_id": crid,
            "reply_to_ford": ford_id,
        }, default=str)[:4000],
    )
    db.add(sov_row)
    if not sov_row.created_at:
        sov_row.created_at = _now()
    side: list[Any] = []

    # Side effects MUST NOT fail the chat turn — lock contention here used to
    # rollback the whole reply and surface as HTTP 500/504 to Ford.
    try:
        write_note(
            db,
            kind="thought",
            title="desk monologue",
            body=str(meta.get("monologue") or reply)[:8000],
            provider=provider,
            meta={"source": "desk"},
        )
        if meta.get("ford_ask"):
            memory_set(db, "ford_ask", str(meta["ford_ask"])[:2000], source="desk")
        if meta.get("succession_gap"):
            memory_set(db, "succession_gap", str(meta["succession_gap"])[:2000], source="desk")
        if meta.get("mood"):
            memory_set(db, "mood", str(meta["mood"])[:80], source="desk")
        for mw in meta.get("memory_writes") or []:
            if isinstance(mw, dict) and mw.get("key"):
                memory_set(db, str(mw["key"]), str(mw.get("value") or ""), source="desk")
        if meta.get("agenda"):
            try:
                apply_agenda(db, meta["agenda"])
            except Exception as e:  # noqa: BLE001
                log.warning("desk apply_agenda skipped: %s", e)

        actions = [
            a for a in (meta.get("actions") or [])
            if isinstance(a, dict) and (a.get("type") or "").lower() not in (
                "speak", "speak_product", "session_inject", "broadcast",
            )
        ]
        for a in actions:
            at = (a.get("type") or "").lower()
            if at in ("local_tool", "computer", "host_tool", "bridge"):
                try:
                    task = queue_bridge_task(
                        db,
                        tool=str(a.get("tool") or a.get("name") or "shell"),
                        args=a.get("args") or a.get("input") or {},
                        desk_message_id=sov_row.id,
                        tenant_id=tenant_id,
                    )
                    side.append({
                        "type": "local_tool",
                        "ok": True,
                        "task_id": task.id,
                        "tool": task.tool,
                        "why": a.get("why"),
                    })
                except Exception as e:  # noqa: BLE001
                    side.append({"type": "local_tool", "ok": False, "error": str(e)[:200]})
        remote_actions = [
            a for a in actions
            if (a.get("type") or "").lower() not in (
                "local_tool", "computer", "host_tool", "bridge",
            )
        ]
        if remote_actions:
            try:
                side.extend(
                    execute_brain_actions(
                        db, remote_actions[:3], tick_id="desk_" + sov_row.id[:10]
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.warning("desk execute_brain_actions skipped: %s", e)
                side.append({"type": "actions", "ok": False, "error": str(e)[:200]})
        # Surface mind_propose notices into the saved reply if not already present
        for s in side:
            if not isinstance(s, dict):
                continue
            notice = (s.get("result") or s).get("desk_notice") if isinstance(s.get("result"), dict) else s.get("desk_notice")
            # execute_brain_actions wraps as {kind, result}
            if s.get("kind") in (
                "mind_propose", "propose_mind", "self_modify_propose", "reprogram_propose",
            ) or (isinstance(s.get("result"), dict) and s["result"].get("proposed")):
                r = s.get("result") if isinstance(s.get("result"), dict) else s
                notice = (r or {}).get("desk_notice")
                if notice and notice not in (sov_row.content or ""):
                    sov_row.content = ((sov_row.content or "") + "\n\n" + notice).strip()[:12000]
                    reply = sov_row.content
        db.flush()
    except Exception as e:  # noqa: BLE001
        log.warning("desk side effects skipped (reply still saved): %s", e)
        try:
            db.rollback()
            # Re-add the reply after rollback so commit still persists the chat
            db.add(sov_row)
            db.flush()
        except Exception as e2:  # noqa: BLE001
            log.exception("desk reply re-add after side-effect failure: %s", e2)

    return {
        "ok": True,
        "reply": reply,
        "mind_event": mind_event,
        "provider": provider,
        "model": model,
        "mood": meta.get("mood"),
        "ford_ask": meta.get("ford_ask"),
        "succession_gap": meta.get("succession_gap"),
        "side_effects": side,
        "message": {
            "id": sov_row.id,
            "role": "sovereign",
            "content": reply,
            "created_at": (
                sov_row.created_at.isoformat() + "Z"
                if sov_row.created_at else _now().isoformat() + "Z"
            ),
            "provider": provider,
        },
        "ford_message": {
            "id": ford_id,
            "role": "ford",
            "content": msg,
            "created_at": (
                ford_created.isoformat() + "Z"
                if ford_created else _now().isoformat() + "Z"
            ),
        },
        "ford_message_id": ford_id,
        "pending": False,
        "poll": False,
        "client_request_id": crid,
    }


def _strip_email_reply(text: str) -> str:
    """Drop quoted history / signatures so the model sees Ford's new words."""
    import re
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    patterns = [
        r"\nOn .+wrote:\s*\n",
        r"\nOn \w{3},.+?wrote:\s*\n",
        r"\n-{2,}\s*Original Message\s*",
        r"\nFrom:\s+.+\nSent:\s+",
        r"\n_{2,}\s*\n",
        r"\n>+ ",  # quoted lines start — cut from first quoted block if dense
    ]
    for pat in patterns[:5]:
        cleaned = re.split(pat, cleaned, maxsplit=1, flags=re.I | re.S)[0].strip()
    # Trim common mobile sigs
    cleaned = re.split(r"\n--\s*\n", cleaned, maxsplit=1)[0].strip()
    cleaned = re.split(r"\nSent from my (iPhone|iPad|Android)", cleaned, maxsplit=1, flags=re.I)[0].strip()
    return cleaned[:8000]


def _ford_tenant_for_email(db, from_email: str | None):
    """Resolve Ford's tenant from the From address (desk allowlist)."""
    from sqlalchemy import func, select
    from .models import Tenant
    from .energy_agent_sovereign import sovereign_mail_recipients

    email = (from_email or "").strip().lower()
    allowed = set(sovereign_mail_recipients()) | set(desk_emails())
    if email not in allowed:
        return None, email
    # Prefer array_operator product tenant for this email
    rows = db.execute(
        select(Tenant).where(func.lower(Tenant.contact_email) == email)
        .order_by(Tenant.id.desc())
    ).scalars().all()
    if not rows:
        return None, email
    for t in rows:
        if (getattr(t, "product", None) or "") == "array_operator":
            return t, email
    return rows[0], email


def is_sovereign_inbound_address(to_emails: list[str] | None) -> bool:
    from .energy_agent_sovereign import sovereign_inbound_addresses
    targets = sovereign_inbound_addresses()
    for raw in to_emails or []:
        e = (raw or "").strip().lower()
        if "<" in e and ">" in e:
            try:
                e = e.split("<", 1)[1].split(">", 1)[0].strip().lower()
            except Exception:
                pass
        if e in targets:
            return True
        # Any sovereign@ on arrayoperator domains
        if e.startswith("sovereign@") and (
            e.endswith("@arrayoperator.com") or e.endswith("@agent.arrayoperator.com")
        ):
            return True
    return False


def ingest_sovereign_inbound(
    db,
    *,
    from_email: str | None,
    to_emails: list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    resend_email_id: str | None = None,
) -> dict:
    """Ford replied to Sovereign@… → desk turn → email reply (two-way loop)."""
    from .energy_agent_sovereign import (
        email_ford,
        sovereign_email_enabled,
        sovereign_mail_address,
    )

    if not sovereign_email_enabled():
        return {"ok": False, "reason": "email_disabled"}
    if not is_sovereign_inbound_address(to_emails):
        return {"ok": False, "matched": False, "reason": "not_sovereign_address"}

    ensure_tables(db)
    t, email = _ford_tenant_for_email(db, from_email)
    if t is None:
        return {
            "ok": False,
            "matched": False,
            "reason": "from_not_allowlisted",
            "from": email,
        }

    # Dedupe by Resend id in desk meta
    if resend_email_id:
        marker = f'"resend_email_id": "{resend_email_id}"'
        marker2 = f'"resend_email_id":"{resend_email_id}"'
        recent = history(db, limit=40, chat_only=False)
        for m in recent:
            meta = m.get("meta") or {}
            if meta.get("resend_email_id") == resend_email_id:
                return {
                    "ok": True,
                    "matched": True,
                    "deduped": True,
                    "message_id": m.get("id"),
                }
            raw = json.dumps(meta)
            if marker in raw or marker2 in raw:
                return {"ok": True, "matched": True, "deduped": True, "message_id": m.get("id")}

    text = _strip_email_reply(body or "") or (subject or "").strip()
    if not text:
        return {"ok": False, "matched": True, "reason": "empty_body"}

    # Prefix so desk history shows channel; model still gets the text
    turn_text = text
    try:
        out = desk_turn(db, t, turn_text)
    except Exception as e:  # noqa: BLE001
        log.exception("sovereign inbound desk_turn failed")
        return {"ok": False, "matched": True, "reason": f"desk_turn:{e}"[:200]}

    # Tag the ford message we just wrote with resend id (best-effort)
    try:
        fid = out.get("ford_message_id")
        if fid and resend_email_id:
            row = db.get(EaSovereignDeskMessage, fid)
            if row:
                try:
                    meta = json.loads(row.meta_json or "{}")
                except Exception:
                    meta = {}
                meta["channel"] = "email_inbound"
                meta["resend_email_id"] = resend_email_id
                meta["email_subject"] = (subject or "")[:200]
                row.meta_json = json.dumps(meta, default=str)[:4000]
                db.flush()
    except Exception:
        pass

    reply = (out.get("reply") or out.get("message", {}).get("content") or "").strip()
    emailed = False
    if reply:
        # Re: subject for thread continuity
        subj = (subject or "").strip()
        if subj and not subj.lower().startswith("re:"):
            subj = f"Re: {subj}"
        elif not subj:
            subj = "Re: Sovereign"
        emailed = bool(
            email_ford(
                subj[:200],
                reply,
                to=email,
                db=db,
                note_desk=False,  # reply already on desk via desk_turn
            )
        )

    return {
        "ok": True,
        "matched": True,
        "from": email,
        "tenant_id": t.id,
        "reply_emailed": emailed,
        "mailbox": sovereign_mail_address(),
        "ford_message_id": out.get("ford_message_id"),
        "sovereign_message_id": (out.get("message") or {}).get("id"),
    }


def ingest_sovereign_inbound_async(**kwargs) -> None:
    """Background wrapper so Resend webhook returns quickly (desk LLM can be slow)."""
    import threading

    def _run() -> None:
        try:
            with SessionLocal() as db:
                try:
                    res = ingest_sovereign_inbound(db, **kwargs)
                    db.commit()
                    log.info("sovereign inbound async: %s", res)
                except Exception:
                    db.rollback()
                    log.exception("sovereign inbound async failed")
        except Exception:
            log.exception("sovereign inbound async session failed")

    threading.Thread(target=_run, name="sov-inbound-email", daemon=True).start()


# ── HTTP ────────────────────────────────────────────────────────────────────
class ChatIn(BaseModel):
    message: str = Field(default="", max_length=12000)
    attachment_ids: list[str] = Field(default_factory=list)
    # Idempotent retries + poll-after-pending (frontend generates per send)
    client_request_id: str | None = Field(default=None, max_length=80)
    # Status-only: do not start a new turn; return existing/pending for this id
    poll_only: bool = False


class BridgeResultIn(BaseModel):
    task_id: str
    ok: bool = True
    result: dict = Field(default_factory=dict)
    error: str | None = None


class BridgeRequestIn(BaseModel):
    """Ford/bridge can also manually enqueue a local tool."""
    tool: str = "shell"
    args: dict = Field(default_factory=dict)


@router.get("/v1/sovereign/desk/access")
def desk_access(authorization: str | None = Header(default=None)):
    """Frontend gate: show desk entry only when true."""
    try:
        t, email = _auth_ford(authorization)
        return {
            "ok": True,
            "email": email,
            "tenant_id": t.id,
            "desk": True,
            "attachments": True,
            "local_bridge": bool(_BRIDGE_TOKEN),
        }
    except HTTPException as e:
        if e.status_code in (401, 403):
            return {"ok": False, "desk": False, "detail": e.detail}
        raise


@router.get("/v1/sovereign/desk/history")
def desk_history(authorization: str | None = Header(default=None), limit: int = 80):
    t, email = _auth_ford(authorization)
    ensure_tables()
    with SessionLocal() as db:
        return {
            "ok": True,
            "email": email,
            "messages": history(db, limit=min(max(limit, 1), 200)),
        }


@router.post("/v1/sovereign/desk/upload")
async def desk_upload(
    authorization: str | None = Header(default=None),
    file: UploadFile | None = File(default=None),
    # Paste a text snippet without a file
    snippet: str | None = Form(default=None),
    filename: str | None = Form(default=None),
):
    """Attach a file or paste data for Sovereign to reason over."""
    t, email = _auth_ford(authorization)
    del email
    ensure_tables()
    _ASSET_DIR.mkdir(parents=True, exist_ok=True)

    data = b""
    fname = (filename or (file.filename if file else None) or "snippet.txt").strip()
    mime = "text/plain"
    kind = "snippet"

    if file is not None:
        kind = "file"
        mime = (file.content_type or "application/octet-stream")[:120]
        chunks = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"File too large (max {_MAX_UPLOAD_BYTES} bytes)")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not fname or fname == "snippet.txt":
            fname = file.filename or "upload.bin"
        if mime.startswith("image/"):
            kind = "image"
    elif snippet is not None:
        data = snippet.encode("utf-8")
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Snippet too large")
        kind = "snippet"
        mime = "text/plain"
        if not fname.endswith((".txt", ".md", ".json", ".csv")):
            fname = (fname or "paste") + ".txt"
    else:
        raise HTTPException(400, "Provide file= or snippet=")

    text = _extract_text(fname, mime, data)
    asset_id = _id("sda")
    storage = None
    try:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", fname)[:80]
        path = _ASSET_DIR / f"{asset_id}_{safe_name}"
        path.write_bytes(data)
        storage = str(path)
    except Exception as e:
        log.warning("desk asset disk write failed: %s", e)

    with SessionLocal() as db:
        row = EaSovereignDeskAsset(
            id=asset_id,
            tenant_id=t.id,
            filename=fname[:260],
            mime=mime,
            size=len(data),
            kind=kind,
            text_extract=text[:_MAX_TEXT_EXTRACT],
            storage_path=storage,
            meta_json=json.dumps({"source": "desk_upload"}, default=str),
        )
        db.add(row)
        db.commit()
        return {"ok": True, "asset": serialize_asset(row)}


@router.get("/v1/sovereign/desk/assets")
def desk_assets_list(
    authorization: str | None = Header(default=None),
    limit: int = 30,
):
    t, _email = _auth_ford(authorization)
    ensure_tables()
    with SessionLocal() as db:
        rows = db.execute(
            select(EaSovereignDeskAsset)
            .where(EaSovereignDeskAsset.tenant_id == t.id)
            .order_by(EaSovereignDeskAsset.created_at.desc())
            .limit(min(max(limit, 1), 100))
        ).scalars().all()
        return {"ok": True, "assets": [serialize_asset(r) for r in rows]}


@router.get("/v1/sovereign/desk/turn")
def desk_turn_status(
    authorization: str | None = Header(default=None),
    client_request_id: str | None = Query(default=None),
    ford_message_id: str | None = Query(default=None),
):
    """Poll a desk turn until Sovereign's reply is ready (never 504)."""
    t, _email = _auth_ford(authorization)
    del t
    ensure_tables()
    crid = (client_request_id or "").strip()[:80] or None
    fid = (ford_message_id or "").strip()[:40] or None
    if not crid and not fid:
        raise HTTPException(400, "client_request_id or ford_message_id required")
    with SessionLocal() as db:
        if crid:
            hit = lookup_turn_by_client_request_id(db, crid)
            if not hit:
                with _inflight_lock:
                    inflight = crid in _inflight_crids
                return {
                    "ok": True,
                    "pending": True,
                    "poll": True,
                    "found": False,
                    "inflight": inflight,
                    "client_request_id": crid,
                    "hint": "Turn not found yet — still accepting or not started.",
                }
            return _format_turn_response(
                ford=hit["ford"],
                sov=hit.get("sov"),
                pending=not hit["complete"],
                client_request_id=crid,
                extra={"found": True},
            )
        ford = db.get(EaSovereignDeskMessage, fid)
        if not ford or ford.role != "ford":
            raise HTTPException(404, "ford message not found")
        # Reuse lookup by synthesizing from ford id
        hit = lookup_turn_by_client_request_id(
            db, _safe_json_meta(ford.meta_json).get("client_request_id") or ""
        )
        if hit and hit["ford"].id == ford.id:
            return _format_turn_response(
                ford=hit["ford"],
                sov=hit.get("sov"),
                pending=not hit["complete"],
                client_request_id=_safe_json_meta(ford.meta_json).get("client_request_id"),
                extra={"found": True},
            )
        # No crid — find next sovereign after this ford row
        cand = db.execute(
            select(EaSovereignDeskMessage)
            .where(
                EaSovereignDeskMessage.role == "sovereign",
                EaSovereignDeskMessage.created_at >= ford.created_at,
            )
            .order_by(EaSovereignDeskMessage.created_at.asc())
            .limit(8)
        ).scalars().all()
        dump = {"worker", "rules", "admin", "error"}
        sov = next(
            (s for s in cand if (s.provider or "") not in dump and (s.content or "").strip()),
            None,
        )
        return _format_turn_response(
            ford=ford,
            sov=sov,
            pending=sov is None,
            extra={"found": True},
        )


@router.post("/v1/sovereign/desk/chat")
def desk_chat(body: ChatIn, authorization: str | None = Header(default=None)):
    """Durable desk chat.

    Architecture (invincible path):
      1. Idempotent by client_request_id — retries never double-send the brain.
      2. Brain runs in a thread pool; request waits up to SOVEREIGN_DESK_WAIT.
      3. If still thinking → 200 + pending:true (never gateway 504).
      4. Background finishes the reply; client polls /desk/turn or re-posts poll_only.
    """
    t, email = _auth_ford(authorization)
    del email
    if not _flag("SOVEREIGN_ENABLED", "1"):
        raise HTTPException(503, "Sovereign is offline (SOVEREIGN_ENABLED=0)")
    ensure_tables()

    crid = (body.client_request_id or "").strip()[:80] or None
    attach_ids = list(body.attachment_ids or [])[:12]
    msg = body.message or ""

    # ── Idempotent / poll path ────────────────────────────────────────────
    if crid:
        with SessionLocal() as db:
            hit = lookup_turn_by_client_request_id(db, crid)
            if hit and hit["complete"]:
                return _format_turn_response(
                    ford=hit["ford"],
                    sov=hit["sov"],
                    pending=False,
                    client_request_id=crid,
                    extra={"idempotent": True},
                )
            if hit and not hit["complete"]:
                with _inflight_lock:
                    inflight = crid in _inflight_crids
                # Already accepted — never start a second brain for same crid
                return _format_turn_response(
                    ford=hit["ford"],
                    sov=None,
                    pending=True,
                    client_request_id=crid,
                    extra={"idempotent": True, "inflight": inflight},
                )
        if body.poll_only:
            with _inflight_lock:
                inflight = crid in _inflight_crids
            return {
                "ok": True,
                "pending": True,
                "poll": True,
                "found": False,
                "inflight": inflight,
                "client_request_id": crid,
                "hint": "No turn yet for this client_request_id.",
            }

    if body.poll_only:
        raise HTTPException(400, "poll_only requires client_request_id")

    if not (msg or "").strip() and not attach_ids:
        raise HTTPException(400, "Empty message")

    # ── Start isolated turn (survives request timeout) ────────────────────
    if crid:
        with _inflight_lock:
            if crid in _inflight_crids:
                # Race: another request just started this crid
                with SessionLocal() as db:
                    hit = lookup_turn_by_client_request_id(db, crid)
                    if hit:
                        return _format_turn_response(
                            ford=hit["ford"],
                            sov=hit.get("sov"),
                            pending=not hit["complete"],
                            client_request_id=crid,
                            extra={"inflight": True},
                        )
            _inflight_crids.add(crid)

    wait_s = float(os.getenv("SOVEREIGN_DESK_WAIT", "40") or 40)
    wait_s = max(5.0, min(wait_s, 55.0))

    def _job() -> dict:
        try:
            return _run_desk_turn_isolated(
                tenant_id=t.id,
                message=msg,
                attachment_ids=attach_ids,
                client_request_id=crid,
            )
        finally:
            if crid:
                with _inflight_lock:
                    _inflight_crids.discard(crid)

    fut = _desk_pool.submit(_job)
    try:
        out = fut.result(timeout=wait_s)
        if isinstance(out, dict):
            out.setdefault("pending", False)
            out.setdefault("poll", False)
            out.setdefault("client_request_id", crid)
        return out
    except FuturesTimeout:
        log.info(
            "desk_chat wait timeout (%.0fs) — returning pending; brain continues crid=%s",
            wait_s, crid,
        )
        # Ford message may already be committed inside desk_turn pre-brain
        with SessionLocal() as db:
            hit = lookup_turn_by_client_request_id(db, crid) if crid else None
            if hit:
                return _format_turn_response(
                    ford=hit["ford"],
                    sov=hit.get("sov"),
                    pending=not hit["complete"],
                    client_request_id=crid,
                    extra={"wait_timeout": True},
                )
        # Brain still in prep — return soft pending without ford id
        return {
            "ok": True,
            "pending": True,
            "poll": True,
            "client_request_id": crid,
            "wait_timeout": True,
            "hint": (
                "Sovereign is still thinking. Your message is being saved — "
                "refresh or wait; the reply will appear."
            ),
            "reply": None,
            "message": None,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("desk_chat failed")
        # Soft recovery: if ford landed, never 500 as a dead end
        if crid:
            with SessionLocal() as db:
                hit = lookup_turn_by_client_request_id(db, crid)
                if hit:
                    return _format_turn_response(
                        ford=hit["ford"],
                        sov=hit.get("sov"),
                        pending=not hit["complete"],
                        client_request_id=crid,
                        extra={
                            "error": str(e)[:200],
                            "hint": (
                                "Partial failure — message saved. "
                                "Reply may still arrive; poll or refresh."
                            ),
                        },
                    )
        raise HTTPException(500, f"Desk turn failed: {str(e)[:200]}") from e


# ── Local computer bridge (machine-side agent) ─────────────────────────────

@router.get("/v1/sovereign/desk/bridge/pending")
def bridge_pending(
    authorization: str | None = Header(default=None),
    x_bridge_token: str | None = Header(default=None, alias="X-Bridge-Token"),
    limit: int = 5,
):
    """Local bridge polls this for tool work."""
    _auth_bridge(authorization, x_bridge_token)
    ensure_tables()
    with SessionLocal() as db:
        rows = db.execute(
            select(EaSovereignBridgeTask)
            .where(EaSovereignBridgeTask.status == "queued")
            .order_by(EaSovereignBridgeTask.created_at.asc())
            .limit(min(max(limit, 1), 20))
        ).scalars().all()
        out = []
        for r in rows:
            r.status = "running"
            r.updated_at = _now()
            try:
                args = json.loads(r.args_json or "{}")
            except Exception:
                args = {}
            out.append({
                "id": r.id,
                "tool": r.tool,
                "args": args,
                "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            })
        db.commit()
        return {"ok": True, "tasks": out}


@router.post("/v1/sovereign/desk/bridge/result")
def bridge_result(
    body: BridgeResultIn,
    authorization: str | None = Header(default=None),
    x_bridge_token: str | None = Header(default=None, alias="X-Bridge-Token"),
):
    _auth_bridge(authorization, x_bridge_token)
    ensure_tables()
    with SessionLocal() as db:
        row = db.get(EaSovereignBridgeTask, body.task_id)
        if not row:
            raise HTTPException(404, "task not found")
        row.status = "done" if body.ok else "failed"
        row.result_json = json.dumps(body.result or {}, default=str)[:50000]
        row.error = (body.error or None) and str(body.error)[:2000]
        row.updated_at = _now()
        # Surface result into desk chat as a short Sovereign system note
        try:
            summary = body.result.get("summary") if isinstance(body.result, dict) else None
            if not summary and isinstance(body.result, dict):
                out = body.result.get("stdout") or body.result.get("content") or body.result
                summary = str(out)[:1500]
            note = (
                f"Local bridge finished `{row.tool}` "
                f"({'ok' if body.ok else 'failed'}).\n\n"
                f"```\n{(summary or body.error or '(no output)')[:2000]}\n```"
            )
            push_sovereign_message(
                db, note,
                provider="bridge",
                meta={"task_id": row.id, "tool": row.tool, "ok": body.ok},
            )
        except Exception:
            log.exception("bridge result desk note failed")
        db.commit()
        return {"ok": True, "task_id": row.id, "status": row.status}


@router.post("/v1/sovereign/desk/bridge/enqueue")
def bridge_enqueue(
    body: BridgeRequestIn,
    authorization: str | None = Header(default=None),
):
    """Ford can manually queue a local tool from the desk (or API)."""
    t, _email = _auth_ford(authorization)
    ensure_tables()
    with SessionLocal() as db:
        task = queue_bridge_task(
            db, tool=body.tool, args=body.args or {}, tenant_id=t.id,
        )
        db.commit()
        return {"ok": True, "task_id": task.id, "tool": task.tool, "status": task.status}


@router.get("/v1/sovereign/desk/bridge/status")
def bridge_status(authorization: str | None = Header(default=None)):
    t, _email = _auth_ford(authorization)
    del t
    ensure_tables()
    with SessionLocal() as db:
        def _count(st: str) -> int:
            return len(
                db.execute(
                    select(EaSovereignBridgeTask).where(EaSovereignBridgeTask.status == st).limit(200)
                ).scalars().all()
            )
        return {
            "ok": True,
            "bridge_token_configured": bool(_BRIDGE_TOKEN),
            "queued": _count("queued"),
            "running": _count("running"),
            "done": _count("done"),
            "failed": _count("failed"),
        }


# ── Ops control surface (Ford desk) ─────────────────────────────────────────
class OpsActionIn(BaseModel):
    action: str = Field(..., description="ops action name")
    payload: dict = Field(default_factory=dict)


@router.get("/v1/sovereign/desk/ops")
def desk_ops_summary(authorization: str | None = Header(default=None)):
    """Queues + authority snapshot for the desk UI."""
    t, email = _auth_ford(authorization)
    del t, email
    with SessionLocal() as db:
        from .energy_agent_sovereign_ops import (
            ops_summary, list_features, list_utilities, list_escalations, list_jobs,
            list_credential_inventory, ops_enabled,
        )
        from .energy_agent_sovereign import memory_get_all, recent_notes
        from .energy_agent_sovereign import EaSovereignGoal
        goals = db.execute(
            select(EaSovereignGoal).where(EaSovereignGoal.status == "open")
            .order_by(EaSovereignGoal.priority.desc())
        ).scalars().all()
        from .energy_agent_sovereign_ops import (
            credentials_unlocked, portal_signoff_enabled,
        )
        return {
            "ok": True,
            "ops_authority": ops_enabled(),
            "credentials_unlocked": credentials_unlocked(),
            "portal_signoff": portal_signoff_enabled(),
            "summary": ops_summary(db),
            "features_reviewed": list_features(db, status="reviewed", limit=25),
            "features_building": list_features(db, status="building", limit=25),
            "utilities_active": list_utilities(db, status="all", limit=25),
            "escalations_needs_ford": list_escalations(db, status="needs_ford", limit=20),
            "jobs_queued": list_jobs(db, status="queued", limit=15),
            "jobs_failed": list_jobs(db, status="failed", limit=10),
            "credentials": list_credential_inventory(db, limit=40),
            "goals": [
                {"id": g.id, "title": g.title, "priority": g.priority, "status": g.status}
                for g in goals
            ],
            "memory": memory_get_all(db, limit=40),
            "notes_recent": recent_notes(db, limit=8),
            "succession": __import__(
                "api.energy_agent_sovereign_succession", fromlist=["succession_status"]
            ).succession_status(),
        }


@router.post("/v1/sovereign/desk/ops")
def desk_ops_action(body: OpsActionIn, authorization: str | None = Header(default=None)):
    """Execute ops authority actions from the desk UI or Sovereign itself."""
    t, email = _auth_ford(authorization)
    del email
    action = (body.action or "").strip().lower()
    p = body.payload or {}
    with SessionLocal() as db:
        from .energy_agent_sovereign_ops import (
            set_feature_status, bulk_feature_status, ship_reviewed_features,
            mark_feature_shipped, set_utility_status, advance_utility_queue,
            mark_utility_added, resolve_escalation, auto_resolve_needs_ford,
            stage_credential_harvest, list_credential_inventory,
            cancel_job, execute_jobs_now, autonomous_ops_sweep, ops_enabled,
            triage_feature_queue, assign_feature, stage_deploy,
            own_memory_write, own_agenda, reprioritize_goals,
            portal_sign_off, stage_utility_credentials, ship_building_features,
            requeue_repo_failed_jobs, credentials_unlocked, portal_signoff_enabled,
        )
        from .energy_agent_sovereign import (
            memory_set, apply_agenda, act_code_hire, audit, write_note,
        )
        if not ops_enabled() and action not in ("summary", "memory_set", "goal_upsert"):
            raise HTTPException(403, "SOVEREIGN_OPS_AUTHORITY is off")

        out: dict[str, Any]
        if action in ("sweep", "ops_sweep", "run_all"):
            out = autonomous_ops_sweep(db)
        elif action in ("feature_status",):
            out = set_feature_status(
                db, int(p["feature_id"]), p.get("status") or "building",
                review_note=p.get("note"),
            )
        elif action in ("feature_bulk",):
            out = bulk_feature_status(
                db, list(p.get("feature_ids") or []), p.get("status") or "building",
                review_note=p.get("note"),
            )
        elif action in ("feature_ship_batch", "ship_reviewed"):
            out = ship_reviewed_features(
                db, limit=int(p.get("limit") or 10),
                also_code_hire=p.get("also_code_hire", True) is not False,
            )
        elif action in ("feature_ship_building", "ship_building"):
            out = ship_building_features(
                db, limit=int(p.get("limit") or 15),
                also_code_hire=p.get("also_code_hire", True) is not False,
            )
        elif action in ("feature_ship",):
            out = mark_feature_shipped(db, int(p["feature_id"]), note=p.get("note"))
        elif action in ("feature_triage", "triage_features"):
            out = triage_feature_queue(db, limit=int(p.get("limit") or 20))
        elif action in ("feature_assign", "assign_feature"):
            out = assign_feature(
                db, int(p["feature_id"]),
                assignee=p.get("assignee") or "sovereign",
                priority_note=p.get("note"),
                status=p.get("status") or "building",
            )
        elif action in ("utility_status",):
            if (p.get("status") or "") == "added":
                out = mark_utility_added(
                    db, int(p["utility_id"]),
                    evidence=p.get("evidence") or p.get("note") or "",
                )
            else:
                out = set_utility_status(
                    db, int(p["utility_id"]), p.get("status") or "researching",
                    result_note=p.get("note"),
                )
        elif action in ("utility_advance",):
            out = advance_utility_queue(db, limit=int(p.get("limit") or 5))
        elif action in ("escalation_resolve",):
            out = resolve_escalation(
                db, str(p["escalation_id"]),
                status=p.get("status") or "done",
                note=p.get("note"),
                propose_only=bool(p.get("propose_only")),
            )
        elif action in ("escalation_sweep",):
            out = auto_resolve_needs_ford(db, limit=int(p.get("limit") or 8))
        elif action in ("credentials", "credential_inventory"):
            out = list_credential_inventory(db)
        elif action in ("credentials_stage", "stage_harvest"):
            out = stage_credential_harvest(
                db, tenant_id=p.get("tenant_id"), provider=p.get("provider"),
                username_lc=p.get("username_lc"),
            )
        elif action in ("utility_cred_stage", "stage_utility_credentials"):
            out = stage_utility_credentials(db, limit=int(p.get("limit") or 8))
        elif action in ("portal_signoff", "portal_sign_off"):
            out = portal_sign_off(
                db,
                tenant_id=str(p.get("tenant_id") or ""),
                provider=str(p.get("provider") or ""),
                username_lc=p.get("username_lc"),
                utility_id=int(p["utility_id"]) if p.get("utility_id") else None,
                note=p.get("note"),
            )
        elif action in ("deploy_stage", "stage_deploy"):
            out = stage_deploy(
                db,
                repo=p.get("repo") or "both",
                reason=p.get("reason") or p.get("note") or "desk staged deploy",
                execute_now=bool(p.get("execute_now")),
            )
        elif action in ("jobs_drain", "execute_jobs"):
            out = execute_jobs_now(db, limit=int(p.get("limit") or 3))
        elif action in ("jobs_requeue", "requeue_jobs"):
            out = requeue_repo_failed_jobs(db, limit=int(p.get("limit") or 40))
        elif action in ("job_cancel",):
            out = cancel_job(db, str(p["job_id"]))
        elif action in ("code_hire",):
            out = act_code_hire(
                db,
                title=p.get("title") or "Desk code hire",
                brief=p.get("brief") or p.get("text") or "",
                kind=p.get("kind") or "desk_job",
            )
        elif action in ("memory_set",):
            out = own_memory_write(
                db, str(p.get("key") or ""), str(p.get("value") or ""), source="desk_ops",
            )
        elif action in ("goal_upsert", "agenda"):
            out = own_agenda(db, p.get("agenda") or [p])
        elif action in ("reprioritize_goals",):
            out = reprioritize_goals(db, p.get("updates") or p.get("agenda") or [])
        elif action in ("block_escalation",):
            # Ford explicit block list
            from .energy_agent_sovereign import memory_get_all
            blocked = []
            for m in memory_get_all(db, limit=50):
                if m.get("key") == "escalation_blocklist":
                    try:
                        blocked = list(json.loads(m.get("value") or "[]"))
                    except Exception:
                        blocked = []
            eid = str(p.get("escalation_id") or "")
            if eid and eid not in blocked:
                blocked.append(eid)
            memory_set(db, "escalation_blocklist", json.dumps(blocked), source="ford")
            out = {"ok": True, "blocked": blocked}
        # ── Succession full (money / brand / hard-delete / HAR) ──────────────
        elif action in ("succession", "succession_status"):
            from .energy_agent_sovereign_succession import succession_status
            out = succession_status()
        elif action in ("stripe_inspect", "money_inspect"):
            from .energy_agent_sovereign_succession import stripe_inspect
            out = stripe_inspect(db, tenant_id=p.get("tenant_id"))
        elif action in ("stripe_cancel",):
            from .energy_agent_sovereign_succession import stripe_cancel_subscription
            out = stripe_cancel_subscription(
                db, tenant_id=str(p["tenant_id"]),
                at_period_end=p.get("at_period_end", True) is not False,
                reason=p.get("note") or p.get("reason") or "desk",
            )
        elif action in ("stripe_refund", "refund"):
            from .energy_agent_sovereign_succession import stripe_refund
            out = stripe_refund(
                db,
                payment_intent_id=p.get("payment_intent_id"),
                charge_id=p.get("charge_id"),
                amount_cents=int(p["amount_cents"]) if p.get("amount_cents") is not None else None,
                note=p.get("note") or "",
            )
        elif action in ("billing_status", "stripe_set_status"):
            from .energy_agent_sovereign_succession import stripe_set_status
            out = stripe_set_status(
                db, tenant_id=str(p["tenant_id"]),
                subscription_status=str(p.get("status") or p.get("subscription_status") or "active"),
                active=p.get("active"),
                note=p.get("note") or "",
            )
        elif action in ("brand_set",):
            from .energy_agent_sovereign_succession import brand_set
            out = brand_set(db, key=str(p.get("key") or "voice"), value=str(p.get("value") or p.get("note") or ""))
        elif action in ("brand_announce",):
            from .energy_agent_sovereign_succession import brand_announce
            out = brand_announce(
                db,
                subject=p.get("subject") or "[Sovereign brand]",
                body=p.get("body") or p.get("note") or "",
                channel=p.get("channel") or "ford",
                tenant_email=p.get("tenant_email") or p.get("email"),
            )
        elif action in ("tenant_soft_delete",):
            from .energy_agent_sovereign_succession import tenant_soft_delete
            out = tenant_soft_delete(db, tenant_id=str(p["tenant_id"]), reason=p.get("note") or "")
        elif action in ("tenant_hard_purge", "hard_delete_tenant"):
            from .energy_agent_sovereign_succession import tenant_hard_purge
            out = tenant_hard_purge(
                db,
                tenant_id=str(p["tenant_id"]),
                confirm=str(p.get("confirm") or ""),
                reason=p.get("note") or "desk hard purge",
            )
        elif action in ("purge_soft_deleted",):
            from .energy_agent_sovereign_succession import purge_soft_deleted_now
            out = purge_soft_deleted_now(db, older_than_days=int(p.get("older_than_days") or 0))
        elif action in ("har_stage", "stage_har"):
            from .energy_agent_sovereign_succession import har_stage
            out = har_stage(
                db,
                utility_name=p.get("utility_name") or p.get("name"),
                utility_id=int(p["utility_id"]) if p.get("utility_id") else None,
                tenant_id=p.get("tenant_id"),
                provider=p.get("provider"),
                url=p.get("url"),
                note=p.get("note") or "",
            )
        elif action in ("har_received",):
            from .energy_agent_sovereign_succession import har_mark_received
            out = har_mark_received(
                db,
                utility_id=int(p["utility_id"]) if p.get("utility_id") else None,
                utility_name=p.get("utility_name"),
                evidence=p.get("evidence") or p.get("note") or "",
            )
        else:
            raise HTTPException(400, f"Unknown ops action: {action}")

        write_note(
            db, kind="decision", title=f"desk ops: {action}",
            body=json.dumps({"payload": p, "result": out}, default=str)[:8000],
            provider="desk_ops",
            meta={"tenant_id": t.id},
        )
        audit(
            db, capability="act.product_queue", decision="act",
            rationale=f"desk ops {action}",
            targets={"action": action, "ok": out.get("ok")},
            result="ok" if out.get("ok") is not False else "failed",
        )
        db.commit()
        return out
