"""Outbound email archive + extremeness monitor.

WHY THIS EXISTS (Ford, 2026-07-19): `ea_email_delivery` records Resend webhook
events and the SUBJECT only — so when a real customer (John Spencer) received
two contradictory alerts 40 minutes apart on his third day, we could see THAT we
had emailed him but not WHAT we said. You cannot audit, or apologise for, an
email whose body you never kept.

Two jobs, one module:

  1. ARCHIVE  — every outbound email, body included, written at the single
     choke point (`notify._send_via_resend`). All sends funnel through there;
     the only other Resend calls in the codebase hit /emails/receiving, which is
     inbound. One hook, total coverage, no per-caller changes.

  2. MONITOR  — scan each email as it goes out for "extremeness": alarm
     language, invented timeframes, shouting, implausible counts, large money,
     contradictions with what we told the SAME person moments ago, bursts, and
     mass blasts. Anything high-severity emails Ford.

DESIGN RULES THIS FILE OBEYS
  * Archiving must NEVER block or break a send. Every entry point is wrapped;
    on any failure we log and return, and the email still goes out.
  * The monitor must never alert on its OWN alerts (infinite loop), and is
    throttled per flag so a systemic problem is one email, not four hundred.
  * Detection is loud, not silent: a flagged email is still SENT. We are
    reporting on ourselves, not censoring outbound mail. Suppressing a real
    alert to look calm is exactly the self-sabotage Ford has banned.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import (Boolean, DateTime, Integer, String, Text, select, func)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, SessionLocal

log = logging.getLogger("email_archive")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── model ────────────────────────────────────────────────────────────────────
class EmailArchive(Base):
    """One row per outbound email, body included.

    Retention: unbounded by design for now — this is the audit trail, and the
    volume is tens of emails/day. `prune_email_archive()` exists for when that
    changes; nothing calls it automatically yet (a silent pruner that eats the
    evidence would defeat the point).
    """
    __tablename__ = "email_archive"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    to_email: Mapped[str] = mapped_column(String(300), index=True)
    cc: Mapped[str | None] = mapped_column(String(400), nullable=True)
    bcc: Mapped[str | None] = mapped_column(String(400), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(300), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(400), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    product: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    resend_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str] = mapped_column(String(8), default="none", index=True)
    flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    attachments: Mapped[str | None] = mapped_column(String(400), nullable=True)


_ensured = False


def ensure_table() -> None:
    """Create email_archive if missing (shared Base → create_all elsewhere also
    picks it up; this makes a cold prod safe without waiting for a migration)."""
    global _ensured
    if _ensured:
        return
    try:
        from .db import engine
        Base.metadata.create_all(bind=engine, tables=[EmailArchive.__table__])
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        log.warning("email_archive: ensure_table failed: %s", exc)


# ── extremeness detection ────────────────────────────────────────────────────
# Grounded in incidents this product actually had, not hypotheticals.

# Paul Bozuwa, 2026-07-18: the digest said an array "went dark overnight" when
# we simply could not see it. Invented duration is the highest-cost failure mode
# we know of, because it reads as authoritative.
_UNVERIFIABLE_TIME = re.compile(
    r"\b(overnight|all week|for weeks|for days|since yesterday|all night|"
    r"for months|all day)\b", re.I)

_ALARM = re.compile(
    r"\b(went dark|going dark|catastroph\w*|emergenc\w*|disaster|critical failure|"
    r"immediately|urgent\w*|severe\w*|dangerous|hazard\w*|failing fast|"
    r"total loss|shut ?down entirely)\b", re.I)

_COUNT = re.compile(r"\b(\d{1,5})\s+(inverters?|arrays?|sites?|offtakers?|accounts?)\b", re.I)
_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
_CAPS = re.compile(r"\b[A-Z]{4,}\b")

# An email to ourselves is not customer-facing and must never trigger a new
# alert about itself — that is the loop that turns one bug into an inbox flood.
_INTERNAL_HINTS = ("[NEPOOL Operator]", "[Array Operator internal]")

LARGE_COUNT = 10
LARGE_MONEY = 10_000.0


def _plain(text: str | None, html: str | None) -> str:
    if text:
        return text
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html)


def is_internal(to_email: str, subject: str | None) -> bool:
    """True for mail we send ourselves (alerts, watchdogs, error reports)."""
    s = subject or ""
    if any(h in s for h in _INTERNAL_HINTS):
        return True
    internal_to = (os.getenv("INTERNAL_ALERT_TO", "") or "").strip().lower()
    return bool(internal_to) and internal_to == (to_email or "").strip().lower()


def scan(to_email: str, subject: str | None, text: str | None,
         html: str | None, *, db=None) -> tuple[str, list[str]]:
    """Return (severity, flags). severity ∈ none|low|high.

    Content checks are cheap regex; the relational checks (contradiction,
    burst, blast) query the archive itself, which is why they need `db`.
    """
    flags: list[str] = []
    body = _plain(text, html)
    hay = f"{subject or ''}\n{body}"

    if _ALARM.search(hay):
        flags.append("alarm_language")
    if _UNVERIFIABLE_TIME.search(hay):
        flags.append("unverifiable_timeframe")

    caps = [w for w in _CAPS.findall(subject or "") if w not in ("NEPOOL", "GMP", "VEC", "SMA", "REC", "RECS")]
    if len(caps) >= 2 or (subject or "").count("!") >= 2:
        flags.append("shouting")

    counts = [int(m.group(1)) for m in _COUNT.finditer(hay)]
    if counts and max(counts) >= LARGE_COUNT:
        flags.append(f"large_count:{max(counts)}")

    monies = []
    for m in _MONEY.finditer(hay):
        try:
            monies.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    if monies and max(monies) >= LARGE_MONEY:
        flags.append(f"large_money:{int(max(monies))}")

    if db is not None:
        try:
            flags.extend(_relational_flags(db, to_email, subject, counts))
        except Exception as exc:  # noqa: BLE001
            log.warning("email_archive: relational scan failed: %s", exc)

    # Severity: things that misinform or overwhelm a human are high. A single
    # big-but-true number is not, on its own, a problem worth waking Ford for.
    high = {"unverifiable_timeframe", "contradiction", "burst", "mass_blast"}
    sev = "none"
    if flags:
        sev = "high" if (set(flags) & high or
                         ("alarm_language" in flags and "shouting" in flags)) else "low"
    return sev, flags


def _relational_flags(db, to_email: str, subject: str | None,
                      counts: list[int]) -> list[str]:
    """Checks that need history: are we contradicting or flooding this person?"""
    out: list[str] = []
    now = _now()
    recent = db.execute(
        select(EmailArchive).where(
            EmailArchive.to_email == to_email,
            EmailArchive.created_at >= now - timedelta(hours=6),
        ).order_by(EmailArchive.created_at.desc()).limit(20)
    ).scalars().all()

    # BURST: the same human hearing from us repeatedly in an hour.
    hour_ago = now - timedelta(hours=1)
    if sum(1 for r in recent if r.created_at and r.created_at.replace(tzinfo=timezone.utc) >= hour_ago) >= 3:
        out.append("burst")

    # CONTRADICTION: the exact Spencer case — "3 inverters need attention" at
    # 11:20, "6 inverters need attention" at 12:00. Same noun, same person,
    # different number, inside a few hours.
    if counts:
        mine = max(counts)
        for r in recent:
            prev = [int(m.group(1)) for m in _COUNT.finditer(f"{r.subject or ''}\n{r.body_text or ''}")]
            if prev and max(prev) != mine:
                out.append("contradiction")
                break

    # MASS BLAST: one subject going to a crowd in an hour.
    if subject:
        n = db.execute(
            select(func.count(func.distinct(EmailArchive.to_email))).where(
                EmailArchive.subject == subject,
                EmailArchive.created_at >= hour_ago,
            )
        ).scalar() or 0
        if n >= 20:
            out.append("mass_blast")
    return out


# ── archive + alert ──────────────────────────────────────────────────────────
def _recent_alert_sent(db, flagkey: str, within_min: int = 60) -> bool:
    """Have we already told Ford about this flag class recently? Throttle so a
    systemic bug is ONE email, not one per victim."""
    cutoff = _now() - timedelta(minutes=within_min)
    row = db.execute(
        select(EmailArchive.id).where(
            EmailArchive.source == "email_monitor",
            EmailArchive.flags.like(f"%{flagkey}%"),
            EmailArchive.created_at >= cutoff,
        ).limit(1)
    ).first()
    return row is not None


def record(*, to_email: str, subject: str | None, html: str | None,
           text: str | None, product: str | None = None,
           source: str | None = None, resend_id: str | None = None,
           ok: bool = True, dry_run: bool = False,
           cc=None, bcc=None, from_addr: str | None = None,
           attachments: list[dict] | None = None) -> None:
    """Archive one outbound email and monitor it. NEVER raises."""
    try:
        ensure_table()
        internal = is_internal(to_email, subject)
        with SessionLocal() as db:
            sev, flags = ("none", [])
            if not internal:
                sev, flags = scan(to_email, subject, text, html, db=db)

            att = None
            if attachments:
                try:
                    att = ", ".join(
                        f"{a.get('filename','?')}({len(a.get('content') or '')}b)"
                        for a in attachments)[:400]
                except Exception:  # noqa: BLE001
                    att = f"{len(attachments)} attachment(s)"

            row = EmailArchive(
                to_email=(to_email or "")[:300],
                cc=(", ".join(cc) if isinstance(cc, list) else cc or None),
                bcc=(", ".join(bcc) if isinstance(bcc, list) else bcc or None),
                from_addr=(from_addr or None),
                subject=(subject or "")[:400],
                body_text=text,
                body_html=html,
                product=product,
                source=source or ("internal_alert" if internal else None),
                resend_id=resend_id,
                ok=bool(ok), dry_run=bool(dry_run),
                severity=sev, flags=(",".join(flags) if flags else None),
                attachments=att,
            )
            db.add(row)
            db.commit()

            if sev == "high" and not internal:
                _alert(db, row, flags)
    except Exception as exc:  # noqa: BLE001
        # An archive failure must never cost us a customer email.
        log.warning("email_archive: record failed (email still sent): %s", exc)


def _alert(db, row: EmailArchive, flags: list[str]) -> None:
    """Tell Ford an extreme email went out. Throttled per flag class."""
    try:
        key = sorted(f.split(":")[0] for f in flags)[0]
        if _recent_alert_sent(db, key):
            log.info("email_archive: %s alert throttled", key)
            return
        from .notify import send_internal_alert
        body = (
            f"An outbound email tripped the extremeness monitor.\n\n"
            f"  flags:    {', '.join(flags)}\n"
            f"  to:       {row.to_email}\n"
            f"  subject:  {row.subject}\n"
            f"  product:  {row.product}\n"
            f"  sent_at:  {row.created_at}\n"
            f"  archive#: {row.id}\n\n"
            f"--- body (first 1500 chars) ---\n"
            f"{(row.body_text or _plain(None, row.body_html))[:1500]}\n\n"
            f"The email WAS sent — this is a report, not a block.\n"
            f"Read the full archive: GET /admin/emails?flagged=1\n"
        )
        send_internal_alert(f"Email monitor: {', '.join(flags)}", body)
        # Mark the alert itself so the throttle can find it.
        row_alert = EmailArchive(
            to_email="(monitor)", subject=f"alert:{key}", source="email_monitor",
            severity="none", flags=",".join(flags), ok=True,
        )
        db.add(row_alert)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("email_archive: alert failed: %s", exc)


# ── pre-flight deliverability ────────────────────────────────────────────────
# Ford, 2026-07-20: we sent two sign-in links to m@rubin.biz — John Spencer's
# partner, a real prospect — and both were undeliverable. rubin.biz publishes a
# NULL MX (`MX 0 .`, RFC 7505), which is the domain owner explicitly declaring
# that it accepts no mail, ever. That is knowable in milliseconds BEFORE we
# send. Ford found out from a postmaster bounce in his own inbox; we shouldn't
# have sent at all.
#
# HARD RULE — FAIL OPEN. Only a DEFINITIVE negative blocks a send:
#   * NXDOMAIN  — the domain does not exist
#   * null MX   — the domain refuses all mail by declaration
# A timeout, a network blip, a malformed response, a resolver outage: SEND
# ANYWAY. Silently dropping a customer email because DNS was slow is exactly
# the self-sabotage that cost a real array 7.5 days of darkness in July. Better
# to send into a maybe-bad address than to invent a reason not to send.
#
# And when we DO block, it is LOUD: archived with a flag and alerted, never a
# quiet return.
_MX_CACHE: dict[str, tuple[bool, str]] = {}
_MX_CACHE_MAX = 2000
DOH_URL = "https://dns.google/resolve"


def domain_accepts_mail(domain: str, *, timeout: float = 4.0) -> tuple[bool, str]:
    """(deliverable, reason). Unknown/error → (True, ...) — fail open."""
    dom = (domain or "").strip().lower().rstrip(".")
    if not dom:
        return True, "no domain parsed"
    if dom in _MX_CACHE:
        return _MX_CACHE[dom]

    import json as _json
    import urllib.request

    def _q(rrtype: str):
        req = urllib.request.Request(
            f"{DOH_URL}?name={dom}&type={rrtype}",
            headers={"accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())

    try:
        d = _q("MX")
        status = d.get("Status")
        if status == 3:  # NXDOMAIN — the domain does not exist
            out = (False, "domain does not exist (NXDOMAIN)")
        else:
            answers = [a.get("data", "") for a in d.get("Answer", [])
                       if a.get("type") == 15 or "data" in a]
            mx = [a.strip() for a in answers if a]
            # RFC 7505 null MX: a single record whose exchange is the root ".".
            if mx and all(m.split()[-1].rstrip(".") == "" for m in mx):
                out = (False, "domain publishes a null MX (RFC 7505) — accepts no email")
            elif mx:
                out = (True, f"MX ok ({len(mx)} record(s))")
            else:
                # No MX is legal: RFC 5321 falls back to the A record.
                a = _q("A")
                if a.get("Status") == 3:
                    out = (False, "domain does not exist (NXDOMAIN)")
                elif a.get("Answer"):
                    out = (True, "no MX, A-record fallback")
                else:
                    # Ambiguous, not proven bad → fail open.
                    out = (True, "no MX and no A record — sending anyway (unproven)")
    except Exception as exc:  # noqa: BLE001
        log.warning("email_archive: MX preflight failed for %s (sending anyway): %s", dom, exc)
        return True, f"dns check failed ({type(exc).__name__}) — failing open"

    if len(_MX_CACHE) < _MX_CACHE_MAX:
        _MX_CACHE[dom] = out
    return out


def preflight(to_email: str) -> tuple[bool, str]:
    """Should we attempt this send? (deliverable, reason). Fails open."""
    addr = (to_email or "").strip()
    if "@" not in addr:
        return True, "unparseable address — failing open"
    return domain_accepts_mail(addr.rsplit("@", 1)[-1])


def record_blocked(to_email: str, subject: str | None, reason: str,
                   source: str | None = None) -> None:
    """A send we refused to make. Archived AND alerted — never silent."""
    try:
        ensure_table()
        with SessionLocal() as db:
            row = EmailArchive(
                to_email=(to_email or "")[:300], subject=(subject or "")[:400],
                source=source, ok=False, dry_run=False,
                severity="high", flags=f"undeliverable_blocked:{reason}"[:400],
                body_text=f"NOT SENT — pre-flight blocked.\nreason: {reason}",
            )
            db.add(row)
            db.commit()
            log.error("email preflight BLOCKED to=%s reason=%s subject=%r",
                      to_email, reason, subject)
            if not is_internal(to_email, subject):
                _alert(db, row, [f"undeliverable_blocked:{reason}"])
    except Exception as exc:  # noqa: BLE001
        log.warning("email_archive: record_blocked failed: %s", exc)


# ── delivery events (bounce / complaint) ─────────────────────────────────────
def on_delivery_event(to_email: str, event: str, subject: str | None,
                      reason: str | None, resend_id: str | None = None) -> None:
    """Called from the Resend webhook. A bounce or spam complaint to a real
    person is a delivery FAILURE and must reach Ford — before this, the archive
    said ok=True (Resend accepted it) and the downstream rejection was invisible
    unless he happened to read a postmaster email."""
    ev = (event or "").lower()
    if ev not in ("bounced", "complained"):
        return
    try:
        ensure_table()
        with SessionLocal() as db:
            # Mark the original archived send as failed, so the archive tells
            # the truth instead of a stale "accepted by Resend".
            row = None
            if resend_id:
                row = db.execute(select(EmailArchive).where(
                    EmailArchive.resend_id == resend_id).limit(1)).scalars().first()
            if row is not None:
                row.ok = False
                row.severity = "high"
                flags = set((row.flags or "").split(",")) - {""}
                flags.add(ev)
                row.flags = ",".join(sorted(flags))
                db.commit()

            if is_internal(to_email, subject or (row.subject if row else None)):
                return  # our own alert bouncing must not spawn more alerts

            marker = row or EmailArchive(
                to_email=(to_email or "")[:300],
                subject=(subject or "")[:400],
                source="delivery_event", ok=False, severity="high",
                flags=ev, resend_id=resend_id,
                body_text=f"{ev} — {reason or 'no reason given'}",
            )
            if row is None:
                db.add(marker)
                db.commit()
            _alert(db, marker, [ev, f"reason:{(reason or 'unknown')[:80]}"])
    except Exception as exc:  # noqa: BLE001
        log.warning("email_archive: on_delivery_event failed: %s", exc)


def prune_email_archive(keep_days: int = 400) -> int:
    """Manual retention. Deliberately NOT scheduled — see class docstring."""
    ensure_table()
    cutoff = _now() - timedelta(days=keep_days)
    with SessionLocal() as db:
        rows = db.execute(
            select(EmailArchive.id).where(EmailArchive.created_at < cutoff)
        ).scalars().all()
        for rid in rows:
            db.delete(db.get(EmailArchive, rid))
        db.commit()
        return len(rows)
