"""Background loops: periodic SimpleFIN sync + rule evaluation/notification.
Started from the FastAPI lifespan; safe to run without any config (no-ops)."""
from __future__ import annotations

import asyncio
import logging

from . import config
from .connectors import simplefin
from .db import session_scope
from .rules.engine import evaluate_rules
from .rules.notify import deliver_firings

log = logging.getLogger("bankai.scheduler")


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


def start_background_tasks() -> list[asyncio.Task]:
    return [
        asyncio.create_task(_sync_loop(), name="bankai-sync"),
        asyncio.create_task(_rules_loop(), name="bankai-rules"),
    ]
