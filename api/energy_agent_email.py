"""Energy Agent ⇄ owner EMAIL channel (Array Operator).

Two halves of one loop:

1. **Weekly check-in** (`run_weekly_checkins`, Mondays via scheduler): an
   AGENTIC email to every active, non-demo Array Operator owner — composed by
   the Energy Agent brain from live fleet/repairs/invoice data ("here's what I
   handled, here's what I noticed, reply and I'll act"), not a fixed template.
   Default ON for every real tenant with a fleet; per-tenant opt-out via a
   signed footer link (no Ford-only allowlist — this is the productized path,
   unlike the dogfood-gated mind insight mail in energy_agent_mind.py).

2. **Reply-to-act** (`ingest_owner_email`): the owner replies to
   agent@agent.arrayoperator.com and the SAME per-tenant agent (_agent_turn,
   same tools, same session memory) answers by email. Every turn is mirrored
   into the open chat session so chat ⇄ email stays one continuous surface,
   exactly like the repair-tech loop in repair_ops.py.

Safety: tenant resolved strictly by contact_email (active, non-demo); loop
guard on our own addresses; auto-reply suppression; daily turn cap; UI-driving
commands are dropped on the email channel (described in words instead).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid
from datetime import timedelta

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from .db import SessionLocal
from .models import Tenant

log = logging.getLogger("energy_agent_email")

router = APIRouter()

# The owner-facing agent mailbox. Same Resend-received domain as repairs@ —
# one MX, the webhook routes by `to`.
OWNER_AGENT_MAILBOX = (
    os.getenv("EA_OWNER_MAILBOX", "agent@agent.arrayoperator.com").strip().lower()
)
OWNER_AGENT_FROM = f"Energy Agent <{OWNER_AGENT_MAILBOX}>"

# Our own outbound identities — never converse with ourselves.
_SELF_ADDRESSES = {
    OWNER_AGENT_MAILBOX,
    "repairs@agent.arrayoperator.com",
    "sovereign@arrayoperator.com",
    "sovereign@agent.arrayoperator.com",
}

_MAX_OWNER_EMAIL_TURNS_PER_DAY = int(os.getenv("EA_OWNER_EMAIL_TURNS_PER_DAY", "20") or 20)
_CHECKIN_MIN_GAP_DAYS = 5.5  # idempotence across restarts / double-fires


def is_owner_agent_address(to_emails: list[str] | None) -> bool:
    for t in to_emails or []:
        if OWNER_AGENT_MAILBOX in str(t or "").strip().lower():
            return True
    return False


# ── opt-out (signed, no login needed) ───────────────────────────────────────

def _optout_secret() -> str:
    return (
        os.getenv("SO_CONFIG_KEY")
        or os.getenv("ADMIN_API_KEY")
        or os.getenv("RESEND_API_KEY")
        or ""
    ).strip()


def _optout_sig(tenant_id: str) -> str:
    sec = _optout_secret()
    if not sec:
        return ""
    return hmac.new(
        sec.encode(), f"ea-checkin-optout:{tenant_id}".encode(), hashlib.sha256
    ).hexdigest()[:24]


def _optout_url(tenant_id: str, *, on: bool) -> str | None:
    sig = _optout_sig(tenant_id)
    if not sig:
        return None
    base = os.getenv("PUBLIC_API_BASE", "https://web-production-49c83.up.railway.app").rstrip("/")
    return f"{base}/v1/energy-agent/checkin/optout?t={tenant_id}&s={sig}&on={'1' if on else '0'}"


def checkin_opted_out(db, tenant_id: str) -> bool:
    try:
        from .energy_agent_mind import _world_get
        world = _world_get(db, tenant_id) or {}
        return (world.get("profile") or {}).get("email_checkin_optout") is True
    except Exception:
        return False


def _set_checkin_optout(db, tenant_id: str, off: bool) -> None:
    """Patch ONLY the optout flag into the STORED profile (world_patch is a
    shallow top-level update — writing a bare {profile:{flag}} would clobber
    the owner's other stored prefs)."""
    from .energy_agent_mind import EaWorldState, _world_patch
    stored: dict = {}
    row = db.get(EaWorldState, tenant_id)
    if row is not None:
        try:
            stored = (json.loads(row.state_json or "{}").get("profile") or {})
        except Exception:
            stored = {}
    stored = {k: v for k, v in stored.items() if not str(k).startswith("_")}
    stored["email_checkin_optout"] = bool(off)
    _world_patch(db, tenant_id, {"profile": stored})


@router.get("/v1/energy-agent/checkin/optout", response_class=HTMLResponse)
def checkin_optout(t: str = Query(...), s: str = Query(...), on: str = Query("1")):
    expected = _optout_sig(t)
    if not expected or not hmac.compare_digest(s or "", expected):
        return HTMLResponse("<h3>Invalid link.</h3>", status_code=403)
    turning_off = on != "0"
    with SessionLocal() as db:
        try:
            _set_checkin_optout(db, t, turning_off)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("checkin optout patch failed tenant=%s", t)
            return HTMLResponse("<h3>Something went wrong — try again.</h3>", status_code=500)
    other = _optout_url(t, on=not turning_off)
    if turning_off:
        msg = "Weekly check-ins are off."
        alt = f'<a href="{other}">Turn them back on</a>' if other else ""
    else:
        msg = "Weekly check-ins are back on."
        alt = f'<a href="{other}">Turn them off</a>' if other else ""
    return HTMLResponse(
        "<div style='font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;"
        "text-align:center;color:#0f172a'>"
        f"<h2 style='margin-bottom:8px'>{msg}</h2>"
        "<p style='color:#475569'>Energy Agent still watches your fleet either way — "
        "this only controls the Monday email.</p>"
        f"<p>{alt}</p></div>"
    )


# ── weekly check-in ─────────────────────────────────────────────────────────

_CHECKIN_SYSTEM = """You are Energy Agent, the solar-fleet operator working for this owner inside Array Operator.
Write their Monday check-in email — the way a sharp, trusted employee writes their boss a
weekly note. You are given ground-truth JSON about their fleet, repairs, and invoices.

Rules:
- PLAIN TEXT only (no markdown, no headers, no bullets with asterisks — use simple lines and "·" if needed).
- 90-180 words. Lead with what matters most (money and broken things first).
- Report what YOU did this week (repair emails sent, replies handled, tickets closed) in first person.
- Then what you noticed (attention arrays with $/mo at stake, weather-adjusted performance, pending invoices).
- NEVER invent numbers — only use what the JSON gives you. If a section has no data, skip it entirely.
- If everything is healthy, say so in two sentences — short all-clears build trust.
- If there is no O&M contact and sites need attention, say you have nobody to email and ask for a contact.
- SETUP OBJECTIVE: if setup_objective.top_gap is present, name that ONE specific gap and offer to close it (e.g. stale data → offer to refresh; no utility login → offer to help add it). If setup_objective.fully_operational is true, don't invent setup work.
- End by inviting a reply: they can answer this email and you will act.
- Sign off exactly:  — Energy Agent
Return STRICT JSON only: {"subject": "...", "body": "..."} .
Subject: specific and calm, e.g. "Fleet check-in: 2 sites need eyes, ~$140/mo at stake" or "Fleet check-in: all clear this week"."""


def _week_repairs_digest(db, tenant_id: str) -> dict:
    from .models import RepairCheckIn, RepairTicket
    cutoff_dt = None
    try:
        from .models import now as _model_now
        cutoff_dt = _model_now() - timedelta(days=7)
    except Exception:
        from datetime import datetime
        cutoff_dt = datetime.utcnow() - timedelta(days=7)
    opened = db.execute(
        select(func.count(RepairTicket.id)).where(
            RepairTicket.tenant_id == tenant_id, RepairTicket.created_at >= cutoff_dt,
        )
    ).scalar() or 0
    resolved = db.execute(
        select(func.count(RepairTicket.id)).where(
            RepairTicket.tenant_id == tenant_id,
            RepairTicket.resolved_at.isnot(None),
            RepairTicket.resolved_at >= cutoff_dt,
        )
    ).scalar() or 0
    waiting = db.execute(
        select(func.count(RepairTicket.id)).where(
            RepairTicket.tenant_id == tenant_id, RepairTicket.status == "waiting_reply",
        )
    ).scalar() or 0
    outbound = db.execute(
        select(func.count(RepairCheckIn.id)).where(
            RepairCheckIn.tenant_id == tenant_id,
            RepairCheckIn.direction == "outbound",
            RepairCheckIn.sent_ok.is_(True),
            RepairCheckIn.created_at >= cutoff_dt,
        )
    ).scalar() or 0
    inbound = db.execute(
        select(func.count(RepairCheckIn.id)).where(
            RepairCheckIn.tenant_id == tenant_id,
            RepairCheckIn.direction == "inbound",
            RepairCheckIn.created_at >= cutoff_dt,
        )
    ).scalar() or 0
    contacts = 0
    try:
        from .repair_ops import list_contacts
        contacts = len(list_contacts(db, tenant_id))
    except Exception:
        pass
    return {
        "tickets_opened_7d": int(opened),
        "tickets_resolved_7d": int(resolved),
        "tickets_waiting_reply": int(waiting),
        "emails_i_sent_7d": int(outbound),
        "tech_replies_handled_7d": int(inbound),
        "service_contacts_on_file": contacts,
    }


def compose_checkin(db, tenant: Tenant) -> dict | None:
    """Gather live ground truth + LLM-compose. Returns {subject, body} or None."""
    from .energy_agent import (
        _call_llm,
        _investigate_attention_tool,
        _list_recent_invoices_tool,
        _production_forecast_tool,
        _usage_cost,
        _charge,
    )

    attention = {}
    forecast = {}
    invoices = {}
    try:
        attention = _investigate_attention_tool(db, tenant, {"limit": 10})
    except Exception as e:
        log.warning("checkin attention failed %s: %s", tenant.id, e)
    try:
        forecast = _production_forecast_tool(db, tenant, {})
    except Exception as e:
        log.warning("checkin forecast failed %s: %s", tenant.id, e)
    try:
        invoices = _list_recent_invoices_tool(db, tenant, {"limit": 6})
    except Exception as e:
        log.warning("checkin invoices failed %s: %s", tenant.id, e)
    repairs = _week_repairs_digest(db, tenant.id)
    setup = {}
    try:
        from .energy_agent import _compute_setup_status
        s = _compute_setup_status(db, tenant)
        setup = {
            "fully_operational": s.get("fully_operational"),
            "data_fresh": s.get("data_fresh"),
            "top_gap": s.get("top_gap"),
        }
    except Exception as e:
        log.warning("checkin setup_status failed %s: %s", tenant.id, e)

    facts = {
        "operator_name": getattr(tenant, "name", None) or getattr(tenant, "company", None),
        "setup_objective": setup,
        "fleet_attention": {
            "arrays_needing_attention": attention.get("count"),
            "attention_units": attention.get("attention_unit_count"),
            "recoverable_usd_month": attention.get("recoverable_usd_month"),
            "brief": (attention.get("brief") or "")[:1200],
            "fleet_summary": attention.get("fleet_summary") or {},
        },
        "weather_adjusted": (
            {
                "ratio_pct_of_expected": (forecast.get("fleet") or {}).get("ratio_pct"),
                "confidence": (forecast.get("fleet") or {}).get("confidence"),
                "window": forecast.get("window"),
            }
            if forecast.get("available")
            else {"unavailable": True}
        ),
        "invoices": {
            "pending_total_usd": invoices.get("pending_total_usd"),
            "sent_last_30d_total_usd": invoices.get("sent_last_30d_total_usd"),
            "recent": (invoices.get("invoices") or [])[:5],
        },
        "repairs_week": repairs,
    }

    # Release the pooled connection before the blocking LLM HTTP call.
    try:
        db.commit()
    except Exception:
        db.rollback()

    out = _call_llm(
        [
            {"role": "system", "content": _CHECKIN_SYSTEM},
            {"role": "user", "content": json.dumps(facts, default=str)},
        ],
        max_tokens=700,
    )
    try:
        _charge(db, tenant.id, _usage_cost(out.get("usage") or {}), "weekly_checkin")
    except Exception:
        pass
    raw = ((out.get("message") or {}).get("content") or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            log.warning("checkin compose unparseable for %s: %r", tenant.id, raw[:200])
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body or len(body) < 40:
        return None
    return {"subject": subject[:180], "body": body[:4000]}


def send_checkin_email(tenant: Tenant, subject: str, body: str) -> bool:
    from .email_skin import render_email_skin
    from .notify import _send_via_resend

    to = (getattr(tenant, "contact_email", None) or "").strip()
    if not to or "@" not in to:
        return False
    off_url = _optout_url(tenant.id, on=True)
    esc = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    body_html = f"<p style='white-space:pre-line'>{esc}</p>"
    footer = "Reply to this email and Energy Agent acts on it."
    if off_url:
        footer += f' · <a href="{off_url}">Stop weekly check-ins</a>'
    html = render_email_skin(
        preheader=body.splitlines()[0][:88] if body else "Your weekly fleet check-in",
        headline="Weekly check-in",
        intro_line="From Energy Agent, your fleet's operator",
        body_html=body_html,
        footer_line=footer,
        product="array_operator",
    )
    text = body + "\n\nReply to this email and I'll act on it."
    if off_url:
        text += f"\nStop weekly check-ins: {off_url}"
    return bool(
        _send_via_resend(
            to=to,
            subject=subject,
            html=html,
            text=text,
            from_addr=OWNER_AGENT_FROM,
            reply_to=OWNER_AGENT_FROM,
            product="array_operator",
        )
    )


_MORNING_NOTE_SYSTEM = (
    "You are Energy Agent, this solar owner's operator. Write a SHORT personal note (2-3 "
    "sentences, ~45 words) that sits at the top of their morning fleet digest, in your voice "
    "— warm, direct, like a good employee's one-line morning heads-up. You're given ground-"
    "truth JSON. Lead with the single thing that matters most today (a flagged site + $ if "
    "any, else a genuine all-clear). If there's a setup gap, mention it in one clause. NEVER "
    "invent numbers — only use the JSON. Plain text, no markdown, no greeting like 'Hi', no "
    "sign-off (the email already brands you). Do NOT restate the whole digest — the visuals "
    "below cover the detail; you're the human line on top. Return ONLY the note text."
)


def compose_morning_note(db, tenant: Tenant, facts: dict) -> str | None:
    """A 2-3 sentence personal note for the top of the morning digest. Returns
    None on any failure so the digest ships template-only."""
    from .energy_agent import _call_llm, _usage_cost, _charge
    try:
        out = _call_llm(
            [
                {"role": "system", "content": _MORNING_NOTE_SYSTEM},
                {"role": "user", "content": json.dumps(facts, default=str)[:3000]},
            ],
            max_tokens=180,
        )
    except Exception as e:
        log.info("morning note compose failed %s: %s", getattr(tenant, "id", "?"), e)
        return None
    try:
        _charge(db, tenant.id, _usage_cost(out.get("usage") or {}), "morning_note")
    except Exception:
        pass
    note = ((out.get("message") or {}).get("content") or "").strip()
    note = re.sub(r"^\s*(hi|hey|good morning)[!,. ]+", "", note, flags=re.I).strip()
    note = note.strip().strip('"').strip()
    if not note or len(note) < 15:
        return None
    return note[:600]


def send_gap_alert_email(tenant: Tenant, setup_status: dict, top_gap: dict) -> bool:
    """Direct, restrained alert when a gap is actively costing money (stale data
    past the urgent window). Same AO skin + opt-out as the weekly check-in."""
    from .email_skin import render_email_skin
    from .notify import _send_via_resend

    to = (getattr(tenant, "contact_email", None) or "").strip()
    if not to or "@" not in to:
        return False
    why = (top_gap.get("why") or "").strip()
    hrs = setup_status.get("hours_since_capture")
    subject = "Your fleet data has gone stale — worth a quick look"
    off_url = _optout_url(tenant.id, on=True)
    body_lines = [
        why,
        "",
        "I can re-arm your cloud logins and re-pull bills to try to refresh it — "
        "just reply and say \"refresh\", or ask me in the app. If it's a device/"
        "extension login, opening Array Operator in your browser will pull it.",
    ]
    body = "\n".join(body_lines)
    esc = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    footer = "Reply and I'll act on it."
    if off_url:
        footer += f' · <a href="{off_url}">Stop these emails</a>'
    html = render_email_skin(
        preheader=(f"Last fresh data ~{round(hrs)}h ago" if hrs else "Fleet data looks stale"),
        headline="Data went stale",
        intro_line="From Energy Agent, your fleet's operator",
        body_html=f"<p style='white-space:pre-line'>{esc}</p>",
        footer_line=footer,
        product="array_operator",
    )
    text = body + ("\n\nStop these emails: " + off_url if off_url else "")
    return bool(
        _send_via_resend(
            to=to, subject=subject, html=html, text=text,
            from_addr=OWNER_AGENT_FROM, reply_to=OWNER_AGENT_FROM, product="array_operator",
        )
    )


def run_weekly_checkins(*, only_tenant_id: str | None = None, force: bool = False) -> dict:
    """Send the Monday check-in to every eligible owner. One short DB session
    per tenant (LLM + Resend HTTP inside — never one session across the loop)."""
    from .models import Array
    sent, skipped, failed = [], [], []

    with SessionLocal() as db:
        q = select(Tenant).where(
            Tenant.product == "array_operator",
            Tenant.active.is_(True),
            Tenant.is_demo.is_(False),
        )
        if only_tenant_id:
            q = select(Tenant).where(Tenant.id == only_tenant_id)
        tenants = db.execute(q.limit(200)).scalars().all()
        tenant_ids = [t.id for t in tenants]

    for tid in tenant_ids:
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, tid)
                if tenant is None:
                    continue
                to = (getattr(tenant, "contact_email", None) or "").strip()
                if not to or "@" not in to:
                    skipped.append((tid, "no_email"))
                    continue
                if checkin_opted_out(db, tid):
                    skipped.append((tid, "opted_out"))
                    continue
                n_arrays = db.execute(
                    select(func.count(Array.id)).where(
                        Array.tenant_id == tid, Array.deleted_at.is_(None),
                    )
                ).scalar() or 0
                if n_arrays == 0:
                    skipped.append((tid, "no_fleet"))
                    continue
                # Idempotence across restarts / manual runs
                from .energy_agent_mind import _world_get, _world_patch
                world = _world_get(db, tid) or {}
                last = world.get("last_weekly_checkin_at")
                if last and not force:
                    from datetime import datetime
                    try:
                        last_dt = datetime.fromisoformat(str(last).rstrip("Z"))
                        from .models import now as _model_now
                        if (_model_now() - last_dt).total_seconds() < _CHECKIN_MIN_GAP_DAYS * 86400:
                            skipped.append((tid, "too_recent"))
                            continue
                    except Exception:
                        pass

                msg = compose_checkin(db, tenant)
                if not msg:
                    failed.append((tid, "compose_failed"))
                    continue
                ok = send_checkin_email(tenant, msg["subject"], msg["body"])
                if not ok:
                    failed.append((tid, "send_failed"))
                    continue
                from .models import now as _model_now
                _world_patch(db, tid, {
                    "last_weekly_checkin_at": _model_now().isoformat() + "Z",
                })
                # Mirror into the open chat session — one continuous surface.
                try:
                    from .repair_ops import _mirror_email_to_chat
                    _mirror_email_to_chat(
                        db, tid,
                        f"I sent your weekly check-in by email: “{msg['subject']}”. "
                        "Reply to it (or ask here) and I'll act.",
                        kind="weekly_checkin",
                    )
                except Exception:
                    pass
                db.commit()
                sent.append(tid)
        except Exception as e:
            log.exception("weekly checkin failed tenant=%s", tid)
            failed.append((tid, repr(e)[:200]))

    out = {"sent": sent, "skipped": skipped, "failed": failed}
    log.info("weekly checkins: %s", out)
    if failed:
        try:
            from .notify import send_internal_alert
            send_internal_alert(
                f"[EnergyAgent] weekly check-in: {len(failed)} failed, {len(sent)} sent",
                json.dumps(out, default=str)[:4000],
            )
        except Exception:
            pass
    return out


# ── inbound: the owner replies ──────────────────────────────────────────────

_AUTOREPLY_RE = re.compile(
    r"(out of office|auto-?reply|automatic reply|vacation respond|do not reply|no-?reply@)",
    re.I,
)
_QUOTED_TAIL_RE = re.compile(
    r"(\r?\n\s*(>|On .{5,80} wrote:|-{2,}\s*Original Message|From:\s).*)$",
    re.S | re.I,
)


def _email_plain(text: str) -> str:
    """Chat replies carry markdown; email clients render it literally. Strip
    the common tokens without touching the content."""
    t = text or ""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"^#{1,4}\s*", "", t, flags=re.M)
    return t


def _strip_reply_body(body: str) -> str:
    txt = (body or "").strip()
    m = _QUOTED_TAIL_RE.search(txt)
    if m:
        txt = txt[: m.start()].strip()
    return txt[:6000]


def _tenant_for_owner_email(db, from_email: str | None) -> Tenant | None:
    e = (from_email or "").strip().lower()
    if not e or "@" not in e:
        return None
    rows = db.execute(
        select(Tenant).where(
            func.lower(Tenant.contact_email) == e,
            Tenant.active.is_(True),
            Tenant.is_demo.is_(False),
        )
    ).scalars().all()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    # Multiple tenants on one email — prefer array_operator, then newest
    ao = [t for t in rows if getattr(t, "product", "") == "array_operator"]
    pool = ao or rows
    return max(pool, key=lambda t: str(getattr(t, "created_at", "")))


def _owner_turns_today(db, tenant_id: str) -> int:
    from .energy_agent_mind import EaEvent
    from .models import now as _model_now
    cutoff = _model_now() - timedelta(hours=24)
    n = db.execute(
        select(func.count(EaEvent.id)).where(
            EaEvent.tenant_id == tenant_id,
            EaEvent.kind == "owner_email_turn",
            EaEvent.created_at >= cutoff,
        )
    ).scalar() or 0
    return int(n)


def _record_owner_turn(
    db, tenant_id: str, session_id: str | None, subject: str | None,
    resend_email_id: str | None = None,
) -> None:
    try:
        from .energy_agent_mind import EaEvent
        db.add(EaEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            kind="owner_email_turn",
            ref_id=(resend_email_id or None),
            summary=(subject or "owner email")[:200],
            consumed=1,  # bookkeeping only — never an interrupt candidate
        ))
        db.flush()
    except Exception:
        log.warning("owner turn event write failed", exc_info=True)


def _already_processed(db, resend_email_id: str | None) -> bool:
    """Webhook + safety-net poller can both see the same message — dedupe on
    the Resend email id so an owner never gets two replies to one email."""
    if not resend_email_id:
        return False
    try:
        from .energy_agent_mind import EaEvent
        hit = db.execute(
            select(EaEvent.id).where(
                EaEvent.kind == "owner_email_turn",
                EaEvent.ref_id == str(resend_email_id),
            ).limit(1)
        ).scalar_one_or_none()
        return hit is not None
    except Exception:
        return False


def ingest_owner_email(
    db,
    *,
    from_email: str | None,
    subject: str | None,
    body: str | None,
    resend_email_id: str | None = None,
) -> dict:
    from .energy_agent import (
        EaMessage,
        EaSession,
        _agent_turn,
        _find_resumable_session,
        _run_tool,
        _NO_RE,
        _YES_RE,
    )

    frm = (from_email or "").strip().lower()
    if not frm or frm in _SELF_ADDRESSES:
        return {"ok": False, "reason": "self_or_empty"}
    if _already_processed(db, resend_email_id):
        return {"ok": True, "deduped": True}
    hay = f"{subject or ''}\n{(body or '')[:400]}"
    if _AUTOREPLY_RE.search(hay):
        return {"ok": False, "reason": "auto_reply"}

    tenant = _tenant_for_owner_email(db, frm)
    if tenant is None:
        log.info("owner email from unknown sender dropped: %s", frm)
        return {"ok": False, "reason": "unknown_sender"}
    if _owner_turns_today(db, tenant.id) >= _MAX_OWNER_EMAIL_TURNS_PER_DAY:
        return {"ok": False, "reason": "daily_cap"}

    text = _strip_reply_body(body or "")
    if not text:
        return {"ok": False, "reason": "empty"}

    _record_owner_turn(db, tenant.id, None, subject, resend_email_id=resend_email_id)

    session = _find_resumable_session(db, tenant.id)
    if session is None:
        session = EaSession(id=uuid.uuid4().hex[:32], tenant_id=tenant.id, status="open")
        db.add(session)
        db.flush()

    # Pending confirm resolved by a plain yes/no reply — same shortcut as chat.
    # (_agent_turn persists the user turn itself; only these shortcut branches
    # add rows of their own.)
    reply_text = None
    pending = json.loads(session.pending_json) if session.pending_json else None
    if pending and (_YES_RE.match(text) or _NO_RE.match(text)):
        db.add(EaMessage(
            session_id=session.id, tenant_id=tenant.id, role="user",
            content=text[:4000],
            meta_json=json.dumps({"channel": "email", "subject": subject or ""})[:2000],
        ))
        db.flush()
    if pending and _YES_RE.match(text):
        session.pending_json = None
        if pending.get("tool") and isinstance(pending.get("args"), dict):
            targs = dict(pending["args"])
            targs["needs_confirm"] = False
            try:
                res = _run_tool(str(pending["tool"]), targs, tenant, session, db, user_text="confirm")
                reply_text = f"Done — {pending['tool']} applied."
                if isinstance(res, dict) and res.get("error"):
                    reply_text = f"I tried, but it failed: {res['error']}"
            except Exception as e:
                reply_text = f"I tried, but it failed: {e}"
        else:
            reply_text = (
                "That change needs a click in the app (it drives the screen) — "
                "open Array Operator and I'll finish it there."
            )
        db.add(EaMessage(
            session_id=session.id, tenant_id=tenant.id, role="assistant",
            content=reply_text,
            meta_json=json.dumps({"channel": "email"})[:200],
        ))
    elif pending and _NO_RE.match(text):
        session.pending_json = None
        reply_text = "Okay — cancelled that change."
        db.add(EaMessage(
            session_id=session.id, tenant_id=tenant.id, role="assistant",
            content=reply_text,
            meta_json=json.dumps({"channel": "email"})[:200],
        ))
    else:
        out = _agent_turn(
            db, tenant, session, text,
            {
                "channel": "email",
                "note": (
                    "This turn arrived BY EMAIL (reply to your check-in). You cannot "
                    "drive the UI — never emit ui_navigate/highlight/tour/fill/click; "
                    "describe where to click instead. Write PLAIN TEXT (no markdown "
                    "asterisks/headers — this renders in an email client). Writes still "
                    "need a confirm: ask, and their reply 'yes' applies it."
                ),
            },
            source="email",
        )
        reply_text = (out.get("reply") or "").strip() or (
            "I hit a snag processing that — try again, or open the app and ask me there."
        )

    # Email the reply back, threaded on the subject.
    reply_text = _email_plain(reply_text)
    subj = (subject or "Your Energy Agent").strip()
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"
    try:
        from .notify import _send_via_resend
        sent = _send_via_resend(
            to=frm,
            subject=subj[:200],
            html=(
                "<div style='font-family:system-ui,sans-serif;line-height:1.55;color:#0f172a'>"
                + reply_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                + "</div>"
            ),
            text=reply_text,
            from_addr=OWNER_AGENT_FROM,
            reply_to=OWNER_AGENT_FROM,
            product="array_operator",
        )
    except Exception as e:
        log.warning("owner email reply send failed: %s", e)
        sent = False

    # No extra chat mirror needed: the turn itself is persisted in the session
    # (user + assistant rows), so the app already shows it on next open.
    return {"ok": True, "tenant_id": tenant.id, "replied": bool(sent)}


def ingest_owner_email_async(**kwargs) -> None:
    """Background wrapper so the Resend webhook returns fast (the agent turn
    can run multiple tool rounds)."""

    def _run() -> None:
        try:
            with SessionLocal() as db:
                try:
                    res = ingest_owner_email(db, **kwargs)
                    db.commit()
                    log.info("owner email async: %s", res)
                except Exception:
                    db.rollback()
                    log.exception("owner email ingest failed")
        except Exception:
            log.exception("owner email session failed")

    threading.Thread(target=_run, daemon=True).start()
