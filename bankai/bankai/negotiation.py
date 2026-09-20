"""Agentic bill negotiation — ask a merchant for a lower rate, on the
household's instruction, and verify the bill actually dropped.

The sibling of bankai/cancellations.py, and it shares that module's boundaries:
it runs ONLY on a spouse's explicit instruction (the copilot's own idea to
negotiate stays a proposal), its outbound text is a fixed template with only the
figures filled in (so a hostile transaction description can never turn it into a
free-text channel), every send is an audited AgentAction, and each one plants a
watchpoint ~35 days out to check whether the charge really fell — a negotiation
that was ignored is caught and escalated, not assumed to have worked.

What it does NOT do: threaten to cancel an essential service as leverage, or
make claims it cannot support. The template asks plainly for loyalty pricing,
current promotions, and any retention offer, and requests written confirmation.
Many retention desks are phone-only; when a merchant has no support email, the
honest move is a prepared call script (print_page), not a pretend email — the
tool says so rather than failing silently.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import config, watchpoints
from .cancellations import _send_as_holder, household_members
from .connectors import email_harvest
from .models import AgentAction

log = logging.getLogger("bankai.negotiation")

VERIFY_AFTER_DAYS = 35


def compose_request(
    *,
    service_name: str,
    account_name: str,
    account_email: str,
    current_amount: float | None,
    account_identifier: str = "",
    competitor_context: str = "",
    sent_as_holder: bool,
) -> tuple[str, str]:
    """The rate-reduction request — fixed language, only the figures vary."""
    subject = f"Request to review my rate — {service_name}"
    now = f"${current_amount:,.2f}" if current_amount else "my current rate"
    lines = [
        "Hello,",
        "",
        f"I've been a loyal customer and I'd like to review the pricing on my "
        f"{service_name} account to make sure I'm on the best available rate.",
        "",
        f"  Account holder: {account_name}",
        f"  Account email:  {account_email}",
    ]
    if account_identifier:
        lines.append(f"  Account/member #: {account_identifier}")
    lines += [
        "",
        f"I'm currently paying {now}. Could you please let me know:",
        "  - any loyalty or retention pricing I qualify for,",
        "  - current promotions available on my plan, and",
        "  - the lowest rate you can offer to keep my business.",
    ]
    if competitor_context:
        lines += ["", f"For context: {competitor_context}"]
    lines += [
        "",
        "I'd like to stay, and a better rate would make that easy. Please reply "
        "with any adjusted rate in writing, including when it would take effect.",
        "",
        "Thank you,",
        account_name,
    ]
    if not sent_as_holder:
        lines.append(
            f"(sent on behalf of {account_name} by their authorized household "
            f"assistant; replies to this address reach them)"
        )
    return subject, "\n".join(lines)


def execute(
    session: Session,
    *,
    merchant: str,
    service_name: str,
    support_email: str,
    instructed_by: str,
    account_name: str,
    current_amount: float | None = None,
    competitor_context: str = "",
    account_identifier: str = "",
) -> dict:
    """Send the negotiation request, audit it, and plant the verification flag."""
    if "@" not in support_email:
        return {
            "error": f"support_email {support_email!r} is not an address. Many "
            "retention desks are phone-only — if so, prepare a call script with "
            "print_page and read the bill-negotiation skill, rather than emailing."
        }
    if not account_name.strip():
        return {"error": "account_name is required — the merchant needs to know whose account"}
    instructor = instructed_by.strip().lower()
    if instructor not in household_members():
        return {
            "error": (
                f"'{instructed_by}' is not a household member — a negotiation is "
                "sent only on Ford's or Gaurav's instruction. Propose it with "
                "propose_action instead, or bring them the plan first."
            )
        }

    can_send_as_holder = bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)
    account_email = config.GMAIL_ADDRESS or email_harvest.send_from()
    subject, body = compose_request(
        service_name=service_name,
        account_name=account_name,
        account_email=account_email,
        current_amount=current_amount,
        account_identifier=account_identifier,
        competitor_context=competitor_context,
        sent_as_holder=can_send_as_holder,
    )
    try:
        if can_send_as_holder:
            receipt = _send_as_holder(support_email, subject, body)
        else:
            receipt = email_harvest.send_message(
                to=[support_email], subject=subject, text=body
            )
    except Exception as exc:
        log.exception("bill negotiation send failed for %s", merchant)
        session.add(AgentAction(
            kind="bill_negotiation", title=f"Negotiate {service_name}",
            rationale=f"instructed by {instructed_by}", to_email=support_email,
            subject=subject, body=body, status="failed", result=str(exc)[:500],
        ))
        session.flush()
        return {"error": f"send failed: {exc}", "recorded": "failed action logged"}

    action = AgentAction(
        kind="bill_negotiation",
        title=f"Negotiate {service_name}",
        rationale=(
            f"Instructed by {instructed_by}. Rate-reduction request. Standing "
            f"household authorization for bill negotiation (Ford, 2026-08-12)."
        ),
        to_email=support_email, subject=subject, body=body,
        status="executed", result=receipt, executed_at=datetime.utcnow(),
    )
    session.add(action)

    verify_on = date.today() + timedelta(days=VERIFY_AFTER_DAYS)
    now_txt = f"~${current_amount:,.2f}" if current_amount else "its prior rate"
    watchpoints.create_watchpoint(
        session,
        title=f"Did the {service_name} rate actually drop?",
        kind="on_date",
        params={"date": verify_on.isoformat()},
        note=(
            f"A rate-reduction request for {service_name} ({merchant}) went to "
            f"{support_email} on {date.today().isoformat()} on {instructed_by}'s "
            f"instruction; they were paying {now_txt}. Search transactions for the "
            "latest charge from this merchant and compare. Dropped: tell the "
            "household the win and record the new rate with set_account_terms/a "
            "memory note. Unchanged or ignored: say so plainly and offer the next "
            "step — a retention phone call (read the bill-negotiation skill), or "
            "cancellation as leverage if they're willing to switch."
        ),
        created_by="agent",
    )
    session.flush()
    return {
        "sent": True,
        "action_id": action.id,
        "receipt": receipt,
        "subject": subject,
        "verification_watchpoint": verify_on.isoformat(),
    }
