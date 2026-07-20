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
