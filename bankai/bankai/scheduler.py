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
from .messaging import thread as chat_thread
from .models import MemoryNote, Property
from .rules.engine import evaluate_rules
from .rules.notify import deliver_firings

log = logging.getLogger("bankai.scheduler")

REVIEW_MARKER_TITLE = "Last monthly review"

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


async def _sync_loop() -> None:
    while True:
        if config.SIMPLEFIN_ACCESS_URL:
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
        asyncio.create_task(_monthly_review_loop(), name="bankai-monthly-review"),
    ]
