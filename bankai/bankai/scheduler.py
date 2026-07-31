"""Background loops: periodic SimpleFIN sync, rule evaluation/notification,
real-estate comps refresh, email document sweeps, and the proactive monthly
review. Started from the FastAPI lifespan; safe to run without any config
(each loop no-ops until its credentials exist)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import select

from . import config, realestate
from .connectors import email_harvest, simplefin
from .db import session_scope
from .agent import chat as agent_chat
from .messaging import thread as chat_thread
from .models import ChatMessage, MemoryNote, Property
from .rules.engine import evaluate_rules
from .rules.notify import deliver_firings
from .watchpoints import (
    STATUS_ARMED,
    STATUS_FIRED,
    Watchpoint,
    build_wake_prompt,
    evaluate_watchpoints,
)

log = logging.getLogger("bankai.scheduler")

REVIEW_MARKER_TITLE = "Last monthly review"

WATCHPOINT_SPEAKER = "watchpoint (scheduled)"

#: Each wake is a full agent turn, so a tick that trips many flags would stall the
#: loop (and, on claude-cli, burn several subprocesses back to back). Overflow is
#: re-armed inside the same transaction rather than left `fired` — a capped-out
#: wake must be deferred, never silently dropped.
MAX_WAKES_PER_TICK = 3

MONTHLY_REVIEW_PROMPT = (
    "(scheduled monthly review — a new month just started. Look back and ahead: "
    "spending_anomalies for last month's spikes and new merchants, cash_flow_forecast "
    "for the road ahead, net worth and how it moved, property values, upcoming bills. "
    "Check your Document intake checklist and pick at most ONE missing item to gently "
    "request. Then write a short, warm monthly check-in addressed to the household — "
    "lead with the one number that matters most.)"
)


def run_rules_once() -> dict:
    with session_scope() as session:
        firings = evaluate_rules(session)
        delivered = deliver_firings(session, firings)
    return {"fired": len(firings), "delivered": delivered}


def _rearm(watchpoint_id: str) -> None:
    """Put a fired watchpoint back on the armed list so the next tick retries it."""
    with session_scope() as session:
        row = session.get(Watchpoint, watchpoint_id)
        if row is not None and row.status == STATUS_FIRED:
            row.status = STATUS_ARMED
            row.fired_at = None


def run_watchpoints_once() -> dict:
    """Fire due watchpoints and wake the copilot in the shared thread for each."""
    # Pass 1: flip statuses and capture the prompts. session_scope commits on exit,
    # so the armed->fired transition is durable BEFORE any agent turn starts — that
    # is what guarantees one wake per flag, and it also releases the SQLite write
    # lock (the agent turn writes to the same DB from the claude-cli MCP process;
    # holding an uncommitted `fired` row across handle_web would deadlock).
    deferred = 0
    with session_scope() as session:
        fired = evaluate_watchpoints(session)
        # Cap before the commit: the overflow goes back to `armed` in this same
        # transaction, so it simply fires again next tick instead of being lost.
        for overflow in fired[MAX_WAKES_PER_TICK:]:
            overflow.status = STATUS_ARMED
            overflow.fired_at = None
            deferred += 1
        pending = [(w.id, build_wake_prompt(w)) for w in fired[:MAX_WAKES_PER_TICK]]

    woken = 0
    for watchpoint_id, prompt in pending:
        try:
            with session_scope() as session:
                chat_thread.handle_web(session, WATCHPOINT_SPEAKER, prompt)
            woken += 1
        except Exception:
            # The turn failed (backend down). Re-arm so the next tick retries,
            # mirroring the monthly review's "marker only after success" rule.
            log.exception("watchpoint %s wake failed; re-arming", watchpoint_id)
            _rearm(watchpoint_id)
    return {"fired": len(pending), "woken": woken, "deferred": deferred}


async def _sync_loop() -> None:
    while True:
        if config.SIMPLEFIN_ACCESS_URLS:
            result = await asyncio.to_thread(simplefin.sync)
            log.info("simplefin sync: %s", result)
        await asyncio.sleep(config.SYNC_INTERVAL_MINUTES * 60)


async def _rules_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_rules_once)
            if result["fired"]:
                log.info("rules: %s", result)
        except Exception:
            log.exception("rules loop error")
        # Its own try: a watchpoint failure must never take rule delivery down.
        try:
            wp_result = await asyncio.to_thread(run_watchpoints_once)
            if wp_result["fired"] or wp_result["deferred"]:
                log.info("watchpoints: %s", wp_result)
        except Exception:
            log.exception("watchpoints loop error")
        await asyncio.sleep(config.RULES_INTERVAL_MINUTES * 60)


def refresh_properties_once() -> list[dict]:
    results = []
    with session_scope() as session:
        for prop in session.execute(select(Property)).scalars().all():
            results.append(realestate.refresh_property(session, prop))
    return results


async def _realestate_loop() -> None:
    while True:
        if config.RENTCAST_API_KEY:
            try:
                results = await asyncio.to_thread(refresh_properties_once)
                if results:
                    log.info("realestate refresh: %s", results)
            except Exception:
                log.exception("realestate loop error")
        await asyncio.sleep(config.REALESTATE_REFRESH_DAYS * 24 * 3600)


def run_email_poll_once() -> dict:
    from .messaging import email_thread

    with session_scope() as session:
        return email_thread.poll_once(session)


async def _email_chat_loop() -> None:
    """Inbound household email -> the shared thread -> a reply to both spouses."""
    from .messaging import email_thread

    while True:
        if email_thread.configured():
            try:
                result = await asyncio.to_thread(run_email_poll_once)
                if result.get("answered"):
                    log.info("email chat: %s", result)
            except Exception:
                log.exception("email chat loop error")
        await asyncio.sleep(config.EMAIL_POLL_SECONDS)


def run_email_harvest_once() -> dict:
    with session_scope() as session:
        return email_harvest.harvest(session)


async def _email_loop() -> None:
    while True:
        if email_harvest.configured():
            try:
                result = await asyncio.to_thread(run_email_harvest_once)
                log.info("email harvest: %s", result)
            except Exception:
                log.exception("email harvest loop error")
        await asyncio.sleep(config.EMAIL_HARVEST_DAYS * 24 * 3600)


TENDING_SPEAKER = "self-directed work"

TENDING_PROMPT = (
    "(no one asked — this is your own initiative. Tend the household's picture: read and "
    "annotate anything in the vault you have not, replace figures a newer statement has "
    "superseded, add statement terms to accounts missing them, retire or move watchpoints "
    "that no longer fit, check goals' pace, refresh comps if stale, reconcile the planning "
    "sheet and publish actuals if it has drifted, and tidy memory notes that have gone stale "
    "or contradict each other. Then either stay silent, or tell them the one thing that "
    "actually warrants their attention.)"
)


def run_tending_once() -> dict:
    """One self-directed maintenance pass.

    The turn runs against the shared thread so its work has full context, but the
    prompt is NOT stored: a housekeeping instruction is not something a household
    member said, and leaving it in the history would teach the copilot that these
    messages come from them. Only a reply worth hearing is kept.
    """
    with session_scope() as session:
        history = chat_thread.build_history(session)
    history.append({"role": "user", "content": TENDING_PROMPT})

    with session_scope() as session:
        reply = agent_chat.run_turn(session, history, channel="tending")

    if agent_chat.is_silence(reply):
        return {"status": "quiet"}
    with session_scope() as session:
        session.add(
            ChatMessage(
                channel="web", role="assistant", speaker="copilot", content=reply
            )
        )
    return {"status": "spoke", "said": reply[:200]}


async def _tending_loop() -> None:
    # A first pass on startup would fire on every restart, so the loop waits out
    # one interval before its first run.
    while True:
        await asyncio.sleep(config.TENDING_INTERVAL_HOURS * 3600)
        try:
            result = await asyncio.to_thread(run_tending_once)
            log.info("tending: %s", result["status"])
        except Exception:
            log.exception("tending loop error")


def monthly_review_action(session, today: date) -> str:
    """Returns 'run' | 'init' | 'skip'. First tick only sets the marker so a
    fresh deploy doesn't fire a surprise review mid-month; after that, the
    review runs once whenever the marker month falls behind the calendar."""
    month = today.strftime("%Y-%m")
    note = session.execute(
        select(MemoryNote).where(MemoryNote.title == REVIEW_MARKER_TITLE)
    ).scalar_one_or_none()
    if note is None:
        return "init"
    return "skip" if note.content.strip() == month else "run"


def _set_review_marker(session, today: date) -> None:
    month = today.strftime("%Y-%m")
    note = session.execute(
        select(MemoryNote).where(MemoryNote.title == REVIEW_MARKER_TITLE)
    ).scalar_one_or_none()
    if note:
        note.content = month
    else:
        session.add(MemoryNote(title=REVIEW_MARKER_TITLE, content=month))


def run_monthly_review_once() -> dict:
    today = date.today()
    with session_scope() as session:
        action = monthly_review_action(session, today)
        if action == "init":
            _set_review_marker(session, today)
            return {"status": "initialized"}
        if action == "skip":
            return {"status": "skip"}
    # The review is a real agent turn into the shared thread; the marker is set
    # only after it succeeds, so a downed backend retries on the next tick.
    with session_scope() as session:
        chat_thread.handle_web(session, "monthly review (scheduled)", MONTHLY_REVIEW_PROMPT)
    with session_scope() as session:
        _set_review_marker(session, today)
    return {"status": "ran"}


async def _monthly_review_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_monthly_review_once)
            if result["status"] == "ran":
                log.info("monthly review posted to the thread")
        except Exception:
            log.exception("monthly review loop error")
        await asyncio.sleep(12 * 3600)


def start_background_tasks() -> list[asyncio.Task]:
    return [
        asyncio.create_task(_sync_loop(), name="bankai-sync"),
        asyncio.create_task(_rules_loop(), name="bankai-rules"),
        asyncio.create_task(_realestate_loop(), name="bankai-realestate"),
        asyncio.create_task(_email_loop(), name="bankai-email"),
        asyncio.create_task(_email_chat_loop(), name="bankai-email-chat"),
        asyncio.create_task(_monthly_review_loop(), name="bankai-monthly-review"),
        asyncio.create_task(_tending_loop(), name="bankai-tending"),
    ]
