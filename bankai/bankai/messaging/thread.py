"""The shared household thread: ONE persistent conversation in the DB, spanning the
dashboard chat and SMS. Every message (web or text) lands in the same history with a
speaker label, so the copilot never loses context no matter where you talk to it.

SMS inbound flow:
  spouse texts the copilot number
    -> stored as a ChatMessage (speaker = their name)
    -> relayed to the other spouse ("Ford: what's our net worth?")
    -> agent runs over the shared history (speaker-prefixed)
    -> reply stored + broadcast to BOTH spouses

Web flow: same storage and history, no SMS broadcast (the dashboard shows it).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import chat as agent_chat
from ..models import ChatMessage
from . import sms

HISTORY_LIMIT = 40

WELCOME_TEXT = (
    "Welcome to BankAI, your household finance copilot. You'll get replies to your "
    "questions plus any account alerts you set up; message frequency varies. "
    "Msg & data rates may apply. Reply HELP for help, STOP to opt out."
)
HELP_TEXT = (
    "BankAI household finance copilot: text questions about your linked accounts or "
    "ask to set up reminders. Msg & data rates may apply. Reply STOP to opt out."
)
# Twilio's default opt-out handling intercepts these upstream; handled here too as
# defense in depth — an opted-out number must never get a reply.
STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "UNSTOP", "YES", "JOIN"}


def build_history(session: Session, limit: int = HISTORY_LIMIT) -> list[dict]:
    """Unified history across all channels, oldest-first, speaker-prefixed."""
    rows = (
        session.execute(
            select(ChatMessage)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    messages: list[dict] = []
    for m in reversed(rows):
        if m.role == "user":
            messages.append({"role": "user", "content": f"[{m.speaker}] {m.content}"})
        else:
            messages.append({"role": "assistant", "content": m.content})
    # History must start with a user turn for the API.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def _sender_number(sender: str) -> str | None:
    return sms.household_phones().get(sender)


def _is_first_contact(session: Session, sender: str) -> bool:
    return (
        session.execute(
            select(ChatMessage.id)
            .where(ChatMessage.channel == "sms", ChatMessage.speaker == sender)
            .limit(1)
        ).scalar_one_or_none()
        is None
    )


def handle_inbound(session: Session, sender: str, text: str) -> str:
    """Process one inbound SMS from a household member; returns the copilot reply."""
    text = text.strip()
    if not text:
        return ""

    keyword = text.upper()
    number = _sender_number(sender)
    if keyword in STOP_KEYWORDS:
        return ""  # never reply to an opt-out
    if keyword == "HELP":
        if number:
            sms.send_sms(number, HELP_TEXT)
        return HELP_TEXT
    first_contact = _is_first_contact(session, sender)
    if first_contact and number:
        sms.send_sms(number, WELCOME_TEXT)
    if keyword in START_KEYWORDS:
        # Bare opt-in keyword: welcome (sent above on first contact) is the reply.
        if not first_contact and number:
            sms.send_sms(number, WELCOME_TEXT)
        return WELCOME_TEXT

    session.add(ChatMessage(channel="sms", role="user", speaker=sender, content=text))
    # Commit before the agent turn: keeps the message durable even if the turn
    # fails, and releases the write lock so claude-cli's MCP server process can
    # write (memory notes, rules) to the same database mid-turn.
    session.commit()

    # Mirror the sender's message to the other spouse so both see the whole thread.
    sms.broadcast(f"{sender}: {text}", exclude=sender)

    history = build_history(session)
    reply = agent_chat.run_turn(session, history, channel="sms")

    session.add(ChatMessage(channel="sms", role="assistant", speaker="copilot", content=reply))
    session.flush()
    sms.broadcast(reply)
    return reply


def handle_web(session: Session, speaker: str, text: str) -> str:
    """Dashboard message into the same shared thread (no SMS broadcast)."""
    text = text.strip()
    if not text:
        return ""
    speaker = (speaker or "Dashboard").strip()[:60]
    session.add(ChatMessage(channel="web", role="user", speaker=speaker, content=text))
    session.commit()  # release the write lock before the agent turn (see handle_inbound)
    history = build_history(session)
    reply = agent_chat.run_turn(session, history, channel="web")
    session.add(ChatMessage(channel="web", role="assistant", speaker="copilot", content=reply))
    session.flush()
    return reply
