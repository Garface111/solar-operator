"""The shared household SMS thread: persistent in the DB (survives restarts), one
conversation for both spouses + the copilot. Inbound flow:

  spouse texts the copilot number
    -> stored as a ChatMessage (speaker = their name)
    -> relayed to the other spouse ("Ford: what's our net worth?")
    -> agent runs over the shared history (speaker-prefixed)
    -> reply stored + broadcast to BOTH spouses
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import chat as agent_chat
from ..models import ChatMessage
from . import sms

HISTORY_LIMIT = 40


def build_history(session: Session, limit: int = HISTORY_LIMIT) -> list[dict]:
    rows = (
        session.execute(
            select(ChatMessage)
            .where(ChatMessage.channel == "sms")
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


def handle_inbound(session: Session, sender: str, text: str) -> str:
    """Process one inbound SMS from a household member; returns the copilot reply."""
    text = text.strip()
    if not text:
        return ""
    session.add(ChatMessage(channel="sms", role="user", speaker=sender, content=text))
    session.flush()

    # Mirror the sender's message to the other spouse so both see the whole thread.
    sms.broadcast(f"{sender}: {text}", exclude=sender)

    history = build_history(session)
    reply, _ = agent_chat.run_turn(session, history, channel="sms")

    session.add(ChatMessage(channel="sms", role="assistant", speaker="copilot", content=reply))
    session.flush()
    sms.broadcast(reply)
    return reply
