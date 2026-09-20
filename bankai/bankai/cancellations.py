"""Agentic subscription cancellation — the copilot's one standing power to act
on the outside world without a dashboard click.

Authorized by Ford in so many words on 2026-08-10 ("can we give it agentic
powers that allow it to cancel subscriptions for us? I authorize this").
The power is deliberately narrow, and every edge of it is enforced HERE, in
code, not in the prompt:

* ONLY on a spouse's instruction. The copilot may execute a cancellation a
  household member asked for (any channel). Its own ideas ("you never use
  Hulu") remain proposals until a spouse says yes.
* NEVER silently for the guarded categories — insurance, health, utilities,
  phone/internet, debt — where a wrong cancellation costs far more than a
  month's fee (a coverage gap, a bricked phone, a credit mark). Those become
  dashboard proposals no matter who asked.
* TEMPLATE-ONLY outbound text. The email is composed here from fixed language;
  the model chooses the merchant and recipient, never the prose. A hostile
  transaction description cannot turn this power into an exfiltration channel.
* SELF-VERIFYING. Every execution plants an on_date watchpoint ~35 days out;
  the copilot of next month checks whether the merchant actually stopped
  charging and escalates (household email + FCBA dispute draft) if not.
* FULLY AUDITED. Every execution is an AgentAction row (status executed, with
  the instruction that authorized it) in the same trail as gated actions.

Transport: prefer sending AS the account holder (Gmail SMTP — merchants act on
mail from the address on the account; replies land in the inbox the copilot
already reads, closing the confirmation loop). Resend from the copilot's own
address is the fallback and says so honestly in the signature.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, watchpoints
from .connectors import email_harvest
from .models import AgentAction, Transaction

log = logging.getLogger("bankai.cancellations")

VERIFY_AFTER_DAYS = 35

#: Categories where cancellation is never a one-word decision. A wrong gym
#: cancellation costs $15; a wrong insurance cancellation costs a coverage gap.
GUARDED_CATEGORIES = {"insurance", "health", "utilities", "mortgage_rent", "debt"}

#: And the merchants whose transaction categories may be wrong or missing but
#: whose names give the stakes away. Substring match, lowercase.
GUARDED_KEYWORDS = (
    "insurance", "progressive", "lemonade", "geico",
    "pharmacy", "cvs", "walgreens", "hims", "hers", "health",
    "verizon", "vzw", "att ", "at&t", "t-mobile", "comcast", "xfinity",
    "pg&e", "pge ", "electric", "water", "utility",
    "klarna", "affirm", "upstart", "santander", "loan", "credit union",
    "mortgage", "chase ach", "escrow",
)


def household_members() -> set[str]:
    from .messaging import email_thread, sms

    names = set(email_thread.household_emails()) | set(sms.household_phones())
    return {n.strip().lower() for n in names if n.strip()}


def recent_merchant_charges(session: Session, merchant: str, days: int = 400) -> list[Transaction]:
    """The merchant's recent charges, by substring on the description."""
    pattern = f"%{merchant.strip()}%"
    return list(session.execute(
        select(Transaction)
        .where(
            Transaction.description.ilike(pattern),
            Transaction.amount < 0,
            Transaction.posted >= date.today() - timedelta(days=days),
        )
        .order_by(Transaction.posted.desc())
        .limit(24)
    ).scalars())


def guard_reason(session: Session, merchant: str) -> str | None:
    """Why this cancellation must go through the dashboard gate, or None."""
    lowered = merchant.lower()
    for keyword in GUARDED_KEYWORDS:
        if keyword in lowered:
            return f"'{merchant}' matches guarded keyword {keyword!r}"
    for txn in recent_merchant_charges(session, merchant):
        if (txn.category or "") in GUARDED_CATEGORIES:
            return (
                f"charges from '{merchant}' are categorized "
                f"'{txn.category}' — a guarded category"
            )
        for keyword in GUARDED_KEYWORDS:
            if keyword in (txn.description or "").lower():
                return f"a '{merchant}' charge matches guarded keyword {keyword!r}"
    return None


def compose_notice(
    *,
    service_name: str,
    account_name: str,
    account_email: str,
    account_identifier: str = "",
    sent_as_holder: bool,
) -> tuple[str, str]:
    """The cancellation notice — fixed language, only identifiers vary."""
    subject = f"Cancellation request — {service_name}"
    lines = [
        "Hello,",
        "",
        f"I am requesting cancellation of the {service_name} subscription/membership "
        f"on the account below, effective immediately:",
        "",
        f"  Account holder: {account_name}",
        f"  Account email:  {account_email}",
    ]
    if account_identifier:
        lines.append(f"  Account/member #: {account_identifier}")
    lines += [
        "",
        "Please stop all future billing to the payment method on file, send written "
        "confirmation of the cancellation to this address, and state the final "
        "billing date if one applies.",
        "",
        "If anything further is required to complete this cancellation, reply to "
        "this email and it will be handled promptly.",
        "",
        "Thank you,",
        account_name,
    ]
    if not sent_as_holder:
        lines += [
            f"(sent on behalf of {account_name} by their authorized household "
            f"assistant; replies to this address reach them)",
        ]
    return subject, "\n".join(lines)


def _send_as_holder(to: str, subject: str, text: str) -> str:
    """From the account holder's own Gmail — the address the merchant knows."""
    message = EmailMessage()
    message["From"] = config.GMAIL_ADDRESS
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    smtp_host = config.IMAP_HOST.replace("imap.", "smtp.", 1)
    with smtplib.SMTP(smtp_host, 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        smtp.send_message(message)
    return f"sent as {config.GMAIL_ADDRESS} via Gmail SMTP to {to}"


def execute(
    session: Session,
    *,
    merchant: str,
    service_name: str,
    support_email: str,
    instructed_by: str,
    account_name: str,
    account_identifier: str = "",
) -> dict:
    """Send the cancellation, audit it, and plant the verification flag.

    Callers must have already applied guard_reason(); this function enforces
    only the authorization boundary (a household member's instruction).
    """
    if "@" not in support_email:
        return {"error": f"support_email {support_email!r} is not an address"}
    if not account_name.strip():
        return {"error": "account_name is required — the merchant needs to know whose account"}
    instructor = instructed_by.strip().lower()
    if instructor not in household_members():
        return {
            "error": (
                f"'{instructed_by}' is not a household member — a cancellation "
                "executes only on Ford's or Gaurav's instruction. Propose it "
                "with propose_action instead."
            )
        }

    can_send_as_holder = bool(config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD)
    account_email = config.GMAIL_ADDRESS or email_harvest.send_from()
    subject, body = compose_notice(
        service_name=service_name,
        account_name=account_name,
        account_email=account_email,
        account_identifier=account_identifier,
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
        log.exception("cancellation send failed for %s", merchant)
        session.add(AgentAction(
            kind="subscription_cancellation", title=f"Cancel {service_name}",
            rationale=f"instructed by {instructed_by}", to_email=support_email,
            subject=subject, body=body, status="failed", result=str(exc)[:500],
        ))
        session.flush()
        return {"error": f"send failed: {exc}", "recorded": "failed action logged"}

    action = AgentAction(
        kind="subscription_cancellation",
        title=f"Cancel {service_name}",
        rationale=(
            f"Instructed by {instructed_by}. Standing household authorization "
            f"for subscription cancellations (Ford, 2026-08-10)."
        ),
        to_email=support_email, subject=subject, body=body,
        status="executed", result=receipt, executed_at=datetime.utcnow(),
    )
    session.add(action)

    verify_on = date.today() + timedelta(days=VERIFY_AFTER_DAYS)
    watchpoints.create_watchpoint(
        session,
        title=f"Verify the {service_name} cancellation stuck",
        kind="on_date",
        params={"date": verify_on.isoformat()},
        note=(
            f"A cancellation notice for {service_name} ({merchant}) went to "
            f"{support_email} on {date.today().isoformat()} on {instructed_by}'s "
            "instruction. Search transactions for any NEW charge from this "
            "merchant since that date. Charged again: tell the household "
            "plainly and draft the dispute (read the consumer-protection "
            "skill — an FCBA billing-error letter). No new charges and a "
            "confirmation email exists (search_email): tell them it stuck and "
            "update the life model."
        ),
        created_by="agent",
    )
    session.flush()
    return {
        "executed": True,
        "action_id": action.id,
        "sent": receipt,
        "subject": subject,
        "verification_watchpoint": verify_on.isoformat(),
    }
