"""Live-update plumbing: what changed, and when.

The dashboard streams changes over Server-Sent Events instead of being refreshed
by hand. The signal is derived from the DATABASE, not from an in-process event
bus, and that is deliberate: writes arrive from at least four places — the web
process, the `claude-cli` backend's MCP server (a SEPARATE process writing the
same SQLite file), the scheduler loops, and inbound email/SMS. Only shared state
sees all of them, so each topic gets a cheap aggregate fingerprint and the stream
pushes whichever topics moved.

Cost is a handful of COUNT/MAX queries per poll against a household-sized
database — trivial next to keeping the browser honest.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .goals import Goal
from .models import (
    Account,
    AgentAction,
    ChatMessage,
    Comp,
    Document,
    MemoryNote,
    Property,
    Rule,
    RuleFiring,
    SyncLog,
    Transaction,
    Valuation,
)
from .watchpoints import Watchpoint

#: topic -> the panels a browser should reload when it moves. The frontend owns
#: the mapping to loader functions; this list is the contract between them.
TOPICS = (
    "accounts",
    "transactions",
    "chat",
    "documents",
    "rules",
    "properties",
    "goals",
    "watchpoints",
    "actions",
    "memories",
    "sync",
)


def _agg(session: Session, model, *columns) -> str:
    """Aggregates over ONE table.

    Selecting aggregates from several tables in a single statement cross-joins
    them, so a count comes back as a product (rules x firings) that can mask a
    real change — each table gets its own statement instead.
    """
    row = session.execute(select(*columns).select_from(model)).one()
    return "|".join("" if v is None else str(v) for v in row)


def _count(session: Session, model) -> str:
    return str(session.execute(select(func.count()).select_from(model)).scalar_one())


def _by_value(session: Session, column) -> str:
    """Exact counts grouped by a column's value.

    Used for status/enabled flags: a length- or max-based fingerprint cannot see
    'armed' -> 'fired' (same length, same row id), so a firing watchpoint would
    never reach the browser. Grouped counts catch every transition.
    """
    rows = session.execute(
        select(column, func.count()).group_by(column).order_by(column)
    ).all()
    return ";".join(f"{value}:{count}" for value, count in rows)


def fingerprints(session: Session) -> dict[str, str]:
    """One short fingerprint per topic; any change means that panel is stale.

    Mutable fields are folded in on purpose — a document whose summary the agent
    just rewrote, or a goal whose status changed, must register as a change even
    though no row was added.
    """
    return {
        # sum(balance) catches a balance move; count catches add/remove
        "accounts": _agg(
            session, Account, func.count(Account.id), func.sum(Account.balance),
            func.max(Account.balance_date),
        ),
        "transactions": _agg(
            session, Transaction, func.count(Transaction.id), func.max(Transaction.id)
        ),
        "chat": _agg(
            session, ChatMessage, func.count(ChatMessage.id), func.max(ChatMessage.id)
        ),
        # length(summary) catches the copilot annotating a document in place
        "documents": _agg(
            session, Document, func.count(Document.id), func.max(Document.id),
            func.sum(func.length(Document.summary)),
        ),
        "rules": (
            _agg(session, Rule, func.count(Rule.id), func.max(Rule.id))
            + "|" + _count(session, RuleFiring)
            + "#" + _by_value(session, Rule.enabled)
        ),
        "properties": (
            _count(session, Property) + "|" + _count(session, Comp)
            + "|" + _count(session, Valuation)
        ),
        "goals": (
            _agg(session, Goal, func.count(Goal.id), func.max(Goal.id))
            + "#" + _by_value(session, Goal.status)
        ),
        "watchpoints": (
            _agg(session, Watchpoint, func.count(Watchpoint.id), func.max(Watchpoint.id),
                 func.max(Watchpoint.fired_at))
            + "#" + _by_value(session, Watchpoint.status)
        ),
        "actions": (
            _agg(session, AgentAction, func.count(AgentAction.id), func.max(AgentAction.id))
            + "#" + _by_value(session, AgentAction.status)
        ),
        "memories": _agg(
            session, MemoryNote, func.count(MemoryNote.id), func.max(MemoryNote.updated_at),
            func.sum(func.length(MemoryNote.content)),
        ),
        "sync": _agg(session, SyncLog, func.count(SyncLog.id), func.max(SyncLog.id)),
    }


def changed_topics(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Topics whose fingerprint moved. A topic missing from `before` (first poll)
    is NOT reported — the browser already loaded everything on page load."""
    if not before:
        return []
    return sorted(k for k, v in after.items() if before.get(k) != v)
