"""Persistent, self-driving projects — how the copilot completes multi-step work.

A turn is a moment; a household's needs span weeks. Initiatives bridge that: the
copilot opens a project with a goal and a plan, and on each free cycle (tending)
it advances the highest-priority active one by a single concrete step, appends
what it did to the worklog, and sets the next action. Nothing is lost between
turns, and nobody has to re-ask.

The safety line is unchanged and load-bearing: an initiative organizes WORK, it
does not widen REACH. Every action a step performs still passes through that
action's own gate. A step can compute, analyze, annotate, update the life model,
draft a code proposal, note a pending expense, or request a document freely; the
moment it wants to do something side-effectful it hits the same approval,
household-only, or spouse-instruction gate it always would.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Initiative

STATUSES = ("active", "blocked", "done", "abandoned")
OPEN_STATUSES = ("active", "blocked")

#: System-prompt budget for the initiative list, in characters.
RENDER_BUDGET_CHARS = 1800
WORKLOG_KEEP_CHARS = 6000


def open_initiative(
    session: Session,
    *,
    title: str,
    goal: str = "",
    plan: str = "",
    next_action: str = "",
    priority: int = 100,
) -> Initiative:
    row = Initiative(
        title=title.strip()[:200],
        goal=goal.strip(),
        plan=plan.strip(),
        next_action=next_action.strip(),
        priority=int(priority),
        status="active",
    )
    session.add(row)
    session.flush()
    return row


def _append_worklog(row: Initiative, entry: str) -> None:
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp}] {entry.strip()}"
    row.worklog = (f"{row.worklog}\n{line}" if row.worklog else line)[-WORKLOG_KEEP_CHARS:]


def update_initiative(
    session: Session,
    initiative_id: str,
    *,
    worklog_entry: str | None = None,
    next_action: str | None = None,
    plan: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    blocked_on: str | None = None,
) -> Initiative | None:
    row = session.get(Initiative, initiative_id)
    if row is None:
        return None
    if worklog_entry and worklog_entry.strip():
        _append_worklog(row, worklog_entry)
    if next_action is not None:
        row.next_action = next_action.strip()
    if plan is not None:
        row.plan = plan.strip()
    if status in STATUSES:
        row.status = status
    if priority is not None:
        row.priority = int(priority)
    if blocked_on is not None:
        row.blocked_on = blocked_on.strip()[:300]
        if row.blocked_on and row.status == "active":
            row.status = "blocked"
    session.flush()
    return row


def active_by_priority(session: Session) -> list[Initiative]:
    return list(session.execute(
        select(Initiative)
        .where(Initiative.status == "active")
        .order_by(Initiative.priority, Initiative.updated_at)
    ).scalars())


def next_to_advance(session: Session) -> Initiative | None:
    rows = active_by_priority(session)
    return rows[0] if rows else None


def as_dicts(session: Session, include_closed: bool = False) -> list[dict]:
    query = select(Initiative).order_by(Initiative.priority, Initiative.updated_at)
    if not include_closed:
        query = query.where(Initiative.status.in_(OPEN_STATUSES))
    out = []
    for r in session.execute(query).scalars():
        out.append({
            "id": r.id,
            "title": r.title,
            "goal": r.goal,
            "plan": r.plan,
            "next_action": r.next_action,
            "status": r.status,
            "priority": r.priority,
            "blocked_on": r.blocked_on,
            "worklog_tail": (r.worklog or "")[-1500:],
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        })
    return out


def render_for_system(session: Session) -> str:
    """Open initiatives as the system prompt carries them — so every turn knows
    what projects are in flight and what each one is waiting on."""
    rows = list(session.execute(
        select(Initiative)
        .where(Initiative.status.in_(OPEN_STATUSES))
        .order_by(Initiative.priority, Initiative.updated_at)
    ).scalars())
    if not rows:
        return ""
    lines: list[str] = []
    spent = 0
    for r in rows:
        tag = "BLOCKED" if r.status == "blocked" else "active"
        line = f"- [{tag}] {r.title}"
        if r.status == "blocked" and r.blocked_on:
            line += f" — waiting on: {r.blocked_on}"
        elif r.next_action:
            line += f" — next: {r.next_action[:120]}"
        if spent + len(line) > RENDER_BUDGET_CHARS:
            lines.append(f"(+{len(rows) - len(lines)} more — list_initiatives)")
            break
        spent += len(line)
        lines.append(line)
    return "\n".join(lines)


def blocked_needs(session: Session) -> list[dict]:
    """Blocked initiatives and what each needs from the household — for surfacing
    in a check-in rather than silently stalling."""
    return [
        {"id": r.id, "title": r.title, "blocked_on": r.blocked_on}
        for r in session.execute(
            select(Initiative).where(Initiative.status == "blocked")
        ).scalars()
        if r.blocked_on
    ]
