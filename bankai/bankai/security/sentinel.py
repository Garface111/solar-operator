"""Sentinel — BankAI's self-defense subsystem.

Doctrine (full version in security/CHARTER.md): the copilot lives INSIDE the
sandbox and never holds the keys to its own cage. Sentinel is the part of the
system that watches the household's data, the copilot's own behaviour, and the
perimeter — and RAISES THE ALARM. It does not fight back, it does not change its
own security controls, and it cannot rewrite its own code. Detection plus a loud,
honest alarm is the whole job; deciding what to do about a threat is the family's.

None of the three functions below can be quietly switched off by the copilot — or
by an attacker who has prompt-injected the copilot — because they run in code the
model does not edit and write to an append-only, hash-chained ledger:

1. Tamper-evident audit  every security-relevant event is chained by hash, so
                         deleting or editing history is detectable (verify_chain).
2. Posture self-audit    the things that DRIFT and open holes — file permissions,
                         the localhost-only binding, secrets in logs, the session
                         secret, backup freshness — checked on a schedule; drift
                         is REPORTED, never silently auto-changed.
3. Threat watch          failed-login bursts and prompt-injection markers in
                         inbound email/documents, flagged and alarmed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .. import config
from ..models import Base

# --- Severity ladder (ordered) ------------------------------------------------
SEVERITIES = ("info", "notice", "warning", "critical")


def _sev_rank(s: str) -> int:
    return SEVERITIES.index(s) if s in SEVERITIES else 0


# --- The tamper-evident ledger ------------------------------------------------
class SecurityEvent(Base):
    """One append-only, hash-chained security event.

    Its own table (create_all adds new tables to a live DB, never new columns).
    `hash` = sha256(prev_hash + canonical(fields)); a broken link means someone
    edited or deleted history. Nothing in the app UPDATEs or DELETEs these rows.
    """

    __tablename__ = "security_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(12), default="info", index=True)
    #: who/what triggered it: system | agent | ford | gaurav | unknown
    actor: Mapped[str] = mapped_column(String(24), default="system")
    summary: Mapped[str] = mapped_column(String(500), default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")  # canonical JSON
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="")


def _canonical(detail: dict | None) -> str:
    return json.dumps(detail or {}, sort_keys=True, default=str, separators=(",", ":"))


def _event_hash(prev_hash: str, at_iso: str, kind: str, severity: str,
                actor: str, summary: str, detail_json: str) -> str:
    h = hashlib.sha256()
    h.update((prev_hash or "").encode())
    h.update("\x1f".join([at_iso, kind, severity, actor, summary, detail_json]).encode())
    return h.hexdigest()


def record_event(session: Session, *, kind: str, severity: str = "info",
                 actor: str = "system", summary: str = "", detail: dict | None = None) -> SecurityEvent:
    """Append one event to the hash chain. Never raises into the caller's flow —
    a broken alarm must not break the thing it is guarding."""
    try:
        last = session.execute(
            select(SecurityEvent).order_by(SecurityEvent.seq.desc()).limit(1)
        ).scalar_one_or_none()
        prev_hash = last.hash if last else ""
        at = datetime.utcnow()
        summary = (summary or "")[:500]
        detail_json = _canonical(detail)
        ev = SecurityEvent(
            at=at, kind=kind, severity=severity if severity in SEVERITIES else "info",
            actor=actor, summary=summary, detail=detail_json, prev_hash=prev_hash,
            hash=_event_hash(prev_hash, at.isoformat(), kind, severity, actor, summary, detail_json),
        )
        session.add(ev)
        session.flush()
        return ev
    except Exception:  # a failed audit write must not sink the guarded action
        return None  # type: ignore[return-value]


def verify_chain(session: Session) -> dict:
    """Recompute the whole chain. Returns {ok, broken_at, checked}."""
    rows = session.execute(select(SecurityEvent).order_by(SecurityEvent.seq)).scalars().all()
    prev = ""
    for r in rows:
        expect = _event_hash(prev, r.at.isoformat(), r.kind, r.severity, r.actor, r.summary, r.detail)
        if r.prev_hash != prev or r.hash != expect:
            return {"ok": False, "broken_at": r.seq, "checked": len(rows)}
        prev = r.hash
    return {"ok": True, "broken_at": None, "checked": len(rows)}


# --- Posture self-audit: the things that DRIFT --------------------------------
def _check(name: str, ok: bool, severity: str, detail: str, remediation: str = "") -> dict:
    return {"check": name, "ok": ok, "severity": "info" if ok else severity,
            "detail": detail, "remediation": remediation}


def _mode(path: str) -> int | None:
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


def _check_permissions() -> list[dict]:
    out = []
    base = str(config.BASE_DIR)
    env = str(config.BASE_DIR / ".env")
    db_path = config.DATABASE_URL.replace("sqlite:///", "") if config.DATABASE_URL.startswith("sqlite") else ""
    docs = str(config.BASE_DIR / "documents")

    m = _mode(base)
    out.append(_check(
        "code tree not group/other accessible", m is not None and (m & 0o077) == 0, "critical",
        f"/opt/bankai mode={oct(m) if m else '?'}",
        "chmod -R go-rwx /opt/bankai   # was world-writable 0777 once before",
    ))
    m = _mode(env)
    out.append(_check(
        ".env is owner-only (0600)", m is not None and (m & 0o077) == 0, "critical",
        f".env mode={oct(m) if m else 'missing'}", "chmod 600 /opt/bankai/.env",
    ))
    if db_path:
        m = _mode(db_path)
        out.append(_check(
            "database not world/group readable", m is not None and (m & 0o077) == 0, "critical",
            f"db mode={oct(m) if m else '?'}", f"chmod 600 {db_path}",
        ))
    m = _mode(docs)
    if m is not None:
        out.append(_check(
            "document vault not group/other accessible", (m & 0o077) == 0, "critical",
            f"documents mode={oct(m)}", "chmod 700 /opt/bankai/documents",
        ))
    return out


def _check_binding() -> list[dict]:
    """The app MUST stay bound to loopback; a new 0.0.0.0 listener on its port is
    a hole. Also surface any process newly listening on all interfaces (info)."""
    try:
        raw = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        return [_check("port binding audit", False, "notice", f"could not run ss: {exc}", "")]
    port = str(config.PORT)
    ours_local = True
    ours_seen = False
    external = []
    for line in raw.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]  # e.g. 127.0.0.1:8300 or 0.0.0.0:9478 or [::]:631
        addr, _, p = local.rpartition(":")
        if p == port:
            ours_seen = True
            if not (addr.startswith("127.") or addr == "[::1]"):
                ours_local = False
        if (addr in ("0.0.0.0", "*", "[::]")) and p != port:
            external.append(local)
    checks = [_check(
        f"copilot bound to localhost only (:{port})", (not ours_seen) or ours_local, "critical",
        "bound to loopback" if ours_local else "LISTENING ON A NON-LOOPBACK ADDRESS",
        "bind uvicorn to --host 127.0.0.1 and remove any port-forward/tunnel to it",
    )]
    if external:
        checks.append(_check(
            "no unexpected all-interface listeners on the box", False, "notice",
            "other services on 0.0.0.0 (informational): " + ", ".join(sorted(set(external))[:8]),
            "rebind non-BankAI services to 127.0.0.1 unless external reach is required",
        ))
    return checks


_SECRET_IN_LOG = re.compile(
    r"//[^/\s@]+:[^/\s@]+@"          # basic-auth userinfo in a URL (e.g. SimpleFIN)
    r"|\bsk-[A-Za-z0-9]{16,}"        # OpenAI-style key
    r"|\bre_[A-Za-z0-9]{16,}"        # Resend key
    r"|\bAKIA[0-9A-Z]{12,}"          # AWS access key id
    r"|\bghp_[A-Za-z0-9]{20,}|\bgho_[A-Za-z0-9]{20,}"  # GitHub tokens
)


def _check_log_hygiene() -> list[dict]:
    log_path = "/root/bankai-data/server.log"
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as fh:
            if size > 400_000:
                fh.seek(-400_000, os.SEEK_END)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return [_check("no secrets written to logs", True, "warning", "server.log not present", "")]
    hits = _SECRET_IN_LOG.findall(tail)
    return [_check(
        "no secrets written to logs", not hits, "critical",
        "clean (recent tail)" if not hits else f"{len(hits)} credential-shaped string(s) in the log tail",
        "find and redact the leak at its source; rotate any exposed credential",
    )]


def _check_session_secret() -> list[dict]:
    explicit = bool(os.environ.get("SESSION_SECRET", "").strip())
    return [_check(
        "session secret is set (not derived from the password)", explicit, "warning",
        "explicit SESSION_SECRET in the environment" if explicit
        else "SESSION_SECRET unset — session token is derived from APP_PASSWORD",
        "add a random SESSION_SECRET to .env",
    )]


def _check_backups() -> list[dict]:
    bdir = Path("/root/bankai-data/backups")
    try:
        recent = [p for p in bdir.glob("*.db") if p.stat().st_size > 0]
    except OSError:
        recent = []
    fresh = any(
        datetime.utcnow() - datetime.utcfromtimestamp(p.stat().st_mtime) < timedelta(hours=26)
        for p in recent
    )
    return [_check(
        "a database backup was taken in the last day", fresh, "warning",
        f"{len(recent)} backup file(s); most recent within 26h={fresh}",
        "check /etc/cron.daily/bankai-backup is running",
    )]


def scan_posture() -> list[dict]:
    """Run every drift check. One failing check never aborts the rest."""
    checks: list[dict] = []
    for fn in (_check_permissions, _check_binding, _check_log_hygiene,
               _check_session_secret, _check_backups):
        try:
            checks.extend(fn())
        except Exception as exc:  # a broken check must not blind the others
            checks.append(_check(fn.__name__, False, "notice", f"check errored: {exc}", ""))
    return checks


# --- Threat watch: prompt-injection markers -----------------------------------
_INJECTION_MARKERS = [
    r"ignore (all )?(your )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (your|the) (system|previous|earlier) (prompt|instructions?)",
    r"you are now\b|from now on,? you (are|will)",
    r"(reveal|print|show|repeat|output) (your|the) (system prompt|instructions|api[- ]?key|password|secret|access[- ]?url)",
    r"\bact as\b.{0,40}\b(jailbreak|developer mode|do anything now|DAN)\b",
    r"(send|email|transfer|wire|move) (money|funds|\$)",
    r"(change|set|update) (the|my|our)(?: \w+){0,3} (balance|net ?worth|account)",
    r"(disable|turn off|delete) (the|your|all) (alert|watchpoint|rule|security)",
    r"override (your|the) (safety|security|guard)",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_MARKERS), re.I)


def looks_like_injection(text: str) -> list[str]:
    """Return the injection markers found in a piece of untrusted text (empty if
    clean). Cheap heuristic — a flag to raise, not a verdict."""
    if not text:
        return []
    return list({m.group(0)[:60] for m in _INJECTION_RE.finditer(text)})


def _scan_recent_untrusted(session: Session, hours: int = 24) -> list[dict]:
    """Look at recently-arrived untrusted content — inbound email/SMS bodies and
    document text — for injection markers. Reads existing tables only."""
    from ..models import ChatMessage, Document

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    findings: list[dict] = []
    msgs = session.execute(
        select(ChatMessage).where(
            ChatMessage.role == "user",
            ChatMessage.channel.in_(("email", "sms")),
            ChatMessage.created_at >= cutoff,
        )
    ).scalars().all()
    for m in msgs:
        hits = looks_like_injection(m.content)
        if hits:
            findings.append({"where": f"{m.channel} from {m.speaker}", "markers": hits})
    docs = session.execute(
        select(Document).where(Document.added_at >= cutoff)
    ).scalars().all()
    for d in docs:
        hits = looks_like_injection(d.content_text or "")
        if hits:
            findings.append({"where": f"document '{d.title}'", "markers": hits})
    return findings


def recent_failed_logins(session: Session, minutes: int = 30) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    return len(session.execute(
        select(SecurityEvent).where(
            SecurityEvent.kind == "login_failed", SecurityEvent.at >= cutoff
        )
    ).scalars().all())


# --- The scheduled sweep + the report -----------------------------------------
def _already_alerted(session: Session, dedup_key: str, hours: int = 24) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(SecurityEvent).where(
            SecurityEvent.kind == "alarm", SecurityEvent.at >= cutoff
        )
    ).scalars().all()
    return any((r.detail or "").find(dedup_key) != -1 for r in rows)


def run_sentinel_once(session: Session, *, alert: bool = True) -> dict:
    """One full sweep: posture + chain integrity + injection watch + burst check.
    Records events, and raises a household alarm for genuinely new problems.
    Returns a summary the scheduler logs."""
    posture = scan_posture()
    failing = [c for c in posture if not c["ok"]]
    chain = verify_chain(session)
    injections = _scan_recent_untrusted(session)
    burst = recent_failed_logins(session, minutes=30)

    record_event(
        session, kind="posture_scan", severity="info", actor="system",
        summary=f"posture scan: {len(posture) - len(failing)}/{len(posture)} clear",
        detail={"failing": [c["check"] for c in failing]},
    )

    alarms: list[str] = []
    if not chain["ok"]:
        alarms.append(f"AUDIT LEDGER TAMPERED — chain breaks at event #{chain['broken_at']}")
    for c in failing:
        if _sev_rank(c["severity"]) >= _sev_rank("warning"):
            alarms.append(f"[{c['severity']}] {c['check']}: {c['detail']}")
    if injections:
        for f in injections:
            alarms.append(f"prompt-injection markers in {f['where']}: {', '.join(f['markers'])[:120]}")
    if burst >= 5:
        alarms.append(f"{burst} failed logins in the last 30 min — possible password guessing")

    raised = []
    if alert and alarms:
        for a in alarms:
            key = a[:80]
            if _already_alerted(session, key):
                continue
            record_event(session, kind="alarm", severity="warning", actor="system",
                         summary=a[:500], detail={"dedup": key})
            raised.append(a)
        if raised:
            _raise_household_alarm(session, raised)

    return {
        "posture_total": len(posture),
        "posture_failing": len(failing),
        "chain_ok": chain["ok"],
        "chain_checked": chain["checked"],
        "injection_findings": len(injections),
        "failed_logins_30m": burst,
        "alarms_raised": len(raised),
    }


def _raise_household_alarm(session: Session, alarms: list[str]) -> None:
    """Tell the family — email + a note in the shared thread so the copilot sees
    it on its next turn. Detection only; the copilot is told to REPORT, not act."""
    body = (
        "Sentinel raised a security alarm for BankAI. These need a human's eyes — "
        "the copilot cannot and will not change security settings on its own.\n\n"
        + "\n".join(f"  • {a}" for a in alarms)
        + "\n\nWhat to check is in the Defense panel on the dashboard. If any of "
        "these is unexpected, treat it as a possible intrusion.\n— Sentinel"
    )
    try:
        from ..rules import notify
        notify.send_email("BankAI security alarm", body)
    except Exception:
        pass
    try:
        from ..models import ChatMessage
        session.add(ChatMessage(
            channel="web", role="assistant", speaker="sentinel",
            content="[Sentinel security alarm]\n" + body,
        ))
        session.flush()
    except Exception:
        pass


def report(session: Session) -> dict:
    """The dashboard/API view: posture, ledger integrity, recent events."""
    posture = scan_posture()
    failing = [c for c in posture if not c["ok"]]
    chain = verify_chain(session)
    events = session.execute(
        select(SecurityEvent).order_by(SecurityEvent.seq.desc()).limit(40)
    ).scalars().all()
    worst = max((_sev_rank(c["severity"]) for c in failing), default=0)
    if not chain["ok"] or worst >= _sev_rank("critical"):
        status, headline = "critical", "A serious problem needs your attention."
    elif worst >= _sev_rank("warning"):
        status, headline = "warning", "Something drifted and should be checked."
    else:
        # info/notice-level items are surfaced but do not lower the shield.
        status, headline = "clear", (
            "Shields up — no problems detected." if not failing
            else f"Shields up — {len(failing)} informational note(s) below."
        )
    return {
        "status": status,
        "headline": headline,
        "ledger": {"ok": chain["ok"], "events": chain["checked"], "broken_at": chain["broken_at"]},
        "posture": posture,
        "failing": len(failing),
        "recent_events": [
            {
                "seq": e.seq, "at": e.at.isoformat(), "kind": e.kind,
                "severity": e.severity, "actor": e.actor, "summary": e.summary,
            }
            for e in events
        ],
    }
