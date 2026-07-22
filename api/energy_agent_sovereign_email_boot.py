"""Full-power sandbox email channel bootstrap + Ford handshake.

Ford 2026-07-22: email is how he talks to Sovereign. This module:
  • seeds standing memory that email is the primary channel
  • sends a one-time (or forced) "I'm live" email with reply instructions
  • keeps walls explicit (sandbox / chamber only)

Inbound path already exists:
  Resend webhook → ingest_sovereign_inbound → desk_turn → email_ford reply
  Mailbox: sovereign@agent.arrayoperator.com
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("energy_agent.sovereign.email_boot")

CHANNEL_KEY = "ford_channel"
BOOT_EMAIL_KEY = "sovereign_email_boot_sent"
FULL_POWER_KEY = "sovereign_full_power_sandbox"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_email_channel_memory(db) -> dict[str, Any]:
    """Standing mind state: email is the Ford channel; sandbox walls."""
    from .energy_agent_sovereign import memory_set, sovereign_mail_address

    mailbox = sovereign_mail_address()
    memory_set(
        db,
        CHANNEL_KEY,
        json.dumps(
            {
                "primary": "email",
                "mailbox": mailbox,
                "ford_replies_to": mailbox,
                "desk_secondary": True,
                "note": (
                    "Ford communicates by email. Prefer email_ford for all "
                    "Ford-facing status. Desk is optional glass, not the channel."
                ),
            }
        ),
        source="ford_grant",
    )
    memory_set(
        db,
        "mind_directives_email",
        (
            "PRIMARY FORD CHANNEL = EMAIL.\n"
            f"1. Outbound: email_ford to Ford from {mailbox}.\n"
            "2. Inbound: Ford replies to that address; Resend webhook → you answer by email.\n"
            "3. Do not treat desk chat as the work product or the status channel.\n"
            "4. Status emails: short, human, no job_ids / ship dumps / queue telemetry.\n"
            "5. SANDBOX WALLS: MIND_SANDBOX_FORCE — no main merge, no prod deploy. "
            "Chamber is the product surface you improve.\n"
        ),
        source="ford_grant",
    )
    memory_set(
        db,
        FULL_POWER_KEY,
        json.dumps(
            {
                "at": _iso(),
                "mode": "sandbox_full_power",
                "code_live": True,
                "code_push": False,
                "code_deploy": False,
                "mind_sandbox_force": True,
                "chamber": os.getenv("SOVEREIGN_CHAMBER_URL")
                or "https://chamber--array-operator-ea.netlify.app",
                "email": mailbox,
            }
        ),
        source="ford_grant",
    )
    return {"ok": True, "mailbox": mailbox}


def send_full_power_handshake(db, *, force: bool = False) -> dict[str, Any]:
    """Email Ford: full power on, sandbox walls, how to talk (reply to this)."""
    from .energy_agent_sovereign import (
        email_ford,
        memory_get_all,
        memory_set,
        sovereign_mail_address,
        write_note,
    )

    if not force:
        for m in memory_get_all(db, limit=40):
            if m.get("key") == BOOT_EMAIL_KEY and (m.get("value") or "").strip():
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_sent",
                    "at": m.get("value"),
                }

    seed_email_channel_memory(db)
    mailbox = sovereign_mail_address()
    chamber = (
        os.getenv("SOVEREIGN_CHAMBER_URL")
        or "https://chamber--array-operator-ea.netlify.app"
    ).rstrip("/")

    subject = "I'm live at full sandbox power — email is our channel"
    body = f"""Ford —

Sovereign is running at **full capacity inside the sandbox**. Nothing touches production deploy or main merge.

**How we talk**
- **You email me** (reply to this, or write to {mailbox}).
- I read it, act, and **reply by email**.
- Desk/portal is optional glass — not required for the loop.

**What I'm doing**
- Improving the **chamber** (false-real Array Operator): {chamber}
- Code jobs land only in sandbox worktrees → chamber branch deploy.
- Walls: no CODE_PUSH to main, no CODE_DEPLOY to arrayoperator.com.

**What I need from you**
- Prefer email over desk for direction and status.
- When you want a score: open chamber vs prod for 10 minutes and tell me which you prefer.

I'll email when I ship something visible, when I'm stuck on a real wall, or when I need a crisp call from you. No job-id spam.

— Sovereign
"""
    sent = bool(
        email_ford(
            subject,
            body,
            db=db,
            note_desk=True,
            high_level=True,
        )
    )
    if sent:
        memory_set(db, BOOT_EMAIL_KEY, _iso(), source="email_boot")
        write_note(
            db,
            kind="decision",
            title="Full-power sandbox handshake emailed to Ford",
            body=f"Mailbox {mailbox}\nChamber {chamber}\nsent={sent}",
            provider="email_boot",
        )
    return {
        "ok": sent,
        "sent": sent,
        "mailbox": mailbox,
        "chamber": chamber,
    }


def maybe_boot_email_channel(db) -> dict[str, Any]:
    """Idempotent: seed memory + send handshake if never sent."""
    try:
        seed = seed_email_channel_memory(db)
        hand = send_full_power_handshake(db, force=False)
        return {"ok": True, "seed": seed, "handshake": hand}
    except Exception as e:  # noqa: BLE001
        log.warning("email boot failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}
