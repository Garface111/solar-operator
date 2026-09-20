"""Background loops: periodic SimpleFIN sync, rule evaluation/notification,
real-estate comps refresh, email document sweeps, and the proactive monthly
review. Started from the FastAPI lifespan; safe to run without any config
(each loop no-ops until its credentials exist)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import select

from . import config, realestate
from .connectors import email_harvest, simplefin
from .db import session_scope
from .agent import chat as agent_chat
from .messaging import thread as chat_thread
from .models import ChatMessage, MemoryNote, Property
from .rules.engine import evaluate_rules
from .rules.notify import deliver_firings
from .security import sentinel
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


SYNC_WAKE_PROMPT = (
    "(fresh bank data just synced — {added} new transaction(s) arrived from the "
    "connected accounts. Read what came in: anything unexpected, a bill posting, "
    "a deposit you were waiting on (check your watchpoints), a charge that "
    "changes this month's picture. Update memory notes if a standing figure "
    "moved. Then either stay silent, or tell the household the one thing in the "
    "new data that actually warrants their attention — in the thread, or with "
    "email_household if it should not wait for them to look.)"
)


def run_sync_wake_once(sync_result: dict) -> dict:
    """Wake the copilot on fresh bank data. Same contract as tending: the
    prompt is never stored, silence is the expected outcome, and only a reply
    worth hearing lands in the thread."""
    added = int(sync_result.get("added") or 0)
    if not config.SYNC_WAKE or added < 1:
        return {"status": "skip"}
    with session_scope() as session:
        history = chat_thread.build_history(session)
    history.append({"role": "user", "content": SYNC_WAKE_PROMPT.format(added=added)})
    with session_scope() as session:
        reply = agent_chat.run_turn(session, history, channel="tending")
    if agent_chat.is_silence(reply):
        return {"status": "quiet", "added": added}
    with session_scope() as session:
        session.add(
            ChatMessage(
                channel="web", role="assistant", speaker="copilot", content=reply
            )
        )
    return {"status": "spoke", "added": added, "said": reply[:200]}


STALE_FEED_MARKER = "Stale feed alerts sent"


def alert_stale_feeds_once(result: dict) -> dict:
    """Tell the household when a bank feed has stopped updating upstream —
    once per stale episode, not every six hours.

    Gaurav's checking sat nine days stale before anyone noticed, because the
    sync 'succeeded' every time (it faithfully stored the provider's old
    snapshot) and SimpleFIN raised no error. The marker note carries which
    accounts have already been announced; an account leaving the stale set
    clears its entry so a future relapse alerts again."""
    import json as _json

    stale = result.get("stale_feeds") or []
    with session_scope() as session:
        note = session.execute(
            select(MemoryNote).where(MemoryNote.title == STALE_FEED_MARKER)
        ).scalar_one_or_none()
        already: dict = {}
        if note is not None:
            try:
                already = _json.loads(note.content or "{}")
            except ValueError:
                already = {}
        current = {s["account"]: s for s in stale}
        new = [current[k] for k in current if k not in already]
        # recovered accounts leave the marker so a relapse re-alerts
        kept = {k: already[k] for k in already if k in current}
        for s in new:
            kept[s["account"]] = date.today().isoformat()
        content = _json.dumps(kept)
        if note is None:
            session.add(MemoryNote(title=STALE_FEED_MARKER, content=content))
        else:
            note.content = content
        if not new:
            return {"status": "quiet", "stale": len(stale)}

    lines = "\n".join(
        f"- {s['account']}" + (f" ({s['institution']})" if s.get("institution") else "")
        + f" — no fresh data for {s['days_stale']:.0f} days"
        for s in new
    )
    body = (
        "One of your bank feeds has stopped updating upstream:\n\n"
        f"{lines}\n\n"
        "BankAI is still syncing fine — the provider itself is serving old data, "
        "which almost always means the bank connection needs to be re-authenticated "
        "at SimpleFIN.\n\n"
        "> [!IMPORTANT]\n"
        "> The fix takes ~2 minutes: sign in at bridge.simplefin.org, find the "
        "affected bank connection, and reconnect it (the bank will likely ask for "
        "a fresh MFA code). Only the account owner can do this — I can't touch "
        "bank logins, by design.\n\n"
        "Until then, figures from this account are frozen at their last good date, "
        "and I'll say so whenever I quote them. I'll keep watching and won't nag "
        "again unless another feed goes stale."
    )
    from .messaging import email_thread

    with session_scope() as session:
        if email_thread.configured():
            email_thread.start_thread(
                session, "A bank feed needs re-authentication", body)
            return {"status": "emailed", "new": [s["account"] for s in new]}
        session.add(ChatMessage(
            channel="web", role="assistant", speaker="copilot", content=body))
    return {"status": "posted", "new": [s["account"] for s in new]}


async def _sync_loop() -> None:
    while True:
        if config.SIMPLEFIN_ACCESS_URLS:
            result = await asyncio.to_thread(simplefin.sync)
            log.info("simplefin sync: %s", result)
            try:
                alerted = await asyncio.to_thread(alert_stale_feeds_once, result)
                if alerted["status"] != "quiet" or result.get("stale_feeds"):
                    log.warning("stale feeds: %s", alerted)
            except Exception:
                log.exception("stale feed alert error")
            try:
                wake = await asyncio.to_thread(run_sync_wake_once, result)
                if wake["status"] != "skip":
                    log.info("sync wake: %s", wake["status"])
            except Exception:
                log.exception("sync wake error")
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


def run_whatsapp_poll_once() -> dict:
    from .messaging import whatsapp_thread

    with session_scope() as session:
        return whatsapp_thread.poll_once(session)


async def _whatsapp_loop() -> None:
    """Household WhatsApp group -> the shared thread -> mostly listening.

    Polled tighter than email: chat has chat expectations, and the expensive
    part (an agent turn) only runs when household messages actually arrived."""
    from .messaging import whatsapp_thread

    while True:
        if whatsapp_thread.configured():
            try:
                result = await asyncio.to_thread(run_whatsapp_poll_once)
                if result.get("stored"):
                    log.info("whatsapp chat: %s", result)
            except Exception:
                log.exception("whatsapp chat loop error")
        await asyncio.sleep(config.WHATSAPP_POLL_SECONDS)


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
    "(no one asked — this is your own initiative. FIRST advance your projects: list_initiatives, "
    "take the next concrete step on the top active one with your tools, and update_initiative "
    "with the progress and next step (or open_initiative for real work worth owning that has "
    "none yet). THEN tend the household's picture: read and annotate anything in the vault you "
    "have not, replace figures a newer statement has superseded, add statement terms to "
    "accounts missing them, retire or move watchpoints that no longer fit, check goals' pace, "
    "refresh comps if stale, reconcile the planning sheet and publish actuals if it has "
    "drifted, and tidy memory notes that have gone stale or contradict each other. Then either "
    "stay silent, or tell them the one thing that actually warrants their attention.)"
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


CHECKIN_MARKER_TITLE = "Last household check-in"

CHECKIN_PROMPT = (
    "(scheduled check-in — a few days have passed since the household last heard "
    "from you directly. Look at what changed: balances and how net worth moved, "
    "anything spending_anomalies flags, bills coming due, goals' pace, watchpoints "
    "near their line. Then write a SHORT check-in addressed to the household — a "
    "few sentences, lead with the one thing that matters most. If everything is "
    "genuinely quiet, say so in a line or two; the check-in arriving IS the point, "
    "so do not stay silent.)"
)


def checkin_action(session, today: date) -> str:
    """Returns 'run' | 'init' | 'skip'. Same first-tick contract as the monthly
    review: a fresh deploy initializes the marker instead of surprise-mailing the
    household; after that the check-in runs whenever the marker date is
    CHECKIN_INTERVAL_DAYS or more behind today."""
    note = session.execute(
        select(MemoryNote).where(MemoryNote.title == CHECKIN_MARKER_TITLE)
    ).scalar_one_or_none()
    if note is None:
        return "init"
    try:
        last = date.fromisoformat(note.content.strip())
    except ValueError:
        return "run"
    return "run" if (today - last).days >= config.CHECKIN_INTERVAL_DAYS else "skip"


def _set_checkin_marker(session, today: date) -> None:
    note = session.execute(
        select(MemoryNote).where(MemoryNote.title == CHECKIN_MARKER_TITLE)
    ).scalar_one_or_none()
    if note:
        note.content = today.isoformat()
    else:
        session.add(MemoryNote(title=CHECKIN_MARKER_TITLE, content=today.isoformat()))


def run_checkin_once() -> dict:
    """One scheduled household check-in: a real agent turn whose reply is emailed
    to both spouses (or posted to the thread while email is dark).

    The prompt is not stored — same reasoning as tending. The marker is set only
    after the check-in actually went out, so a downed backend or a failed send is
    retried on the next tick rather than silently skipping a cycle."""
    if config.CHECKIN_INTERVAL_DAYS <= 0:
        return {"status": "disabled"}
    today = date.today()
    with session_scope() as session:
        action = checkin_action(session, today)
        if action == "init":
            _set_checkin_marker(session, today)
            return {"status": "initialized"}
        if action == "skip":
            return {"status": "skip"}

    with session_scope() as session:
        history = chat_thread.build_history(session)
    history.append({"role": "user", "content": CHECKIN_PROMPT})
    with session_scope() as session:
        # A check-in is a deliberate analytical note to the household — Fable-max.
        reply = agent_chat.run_turn(session, history, channel="web", force_tier="complex")

    if agent_chat.is_silence(reply):
        # The prompt forbids silence, but never put words in the copilot's mouth:
        # count the cycle, log it honestly, and let the next one run on schedule.
        with session_scope() as session:
            _set_checkin_marker(session, today)
        return {"status": "quiet"}

    from .messaging import email_thread

    with session_scope() as session:
        if email_thread.configured():
            email_thread.start_thread(
                session, f"Household check-in — {today.isoformat()}", reply
            )
        else:
            session.add(
                ChatMessage(
                    channel="web", role="assistant", speaker="copilot", content=reply
                )
            )
    with session_scope() as session:
        _set_checkin_marker(session, today)
    return {"status": "sent", "said": reply[:200]}


async def _checkin_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_checkin_once)
            if result["status"] in ("sent", "quiet"):
                log.info("household check-in: %s", result["status"])
        except Exception:
            log.exception("check-in loop error")
        await asyncio.sleep(6 * 3600)


LIFE_REVIEW_MARKER_TITLE = "Last life review"

LIFE_REVIEW_PROMPT = (
    "(scheduled life review — private work, nobody is waiting on a reply. Read the last few "
    "weeks of data the way a detective reads a diary: transactions across every account, "
    "recurring bills, anomalies, pending mentions, the thread. Then work your LIFE MODEL with "
    "your tools: record what happened as events, name the rhythms, check every open "
    "prediction against what actually came true — confirm or refute honestly — and record "
    "the opportunities you can own. Sharpen or retire stale facts. If, and only if, this "
    "review surfaces something the household should hear now, say it or use email_household; "
    "otherwise reply with silence — the updated model IS the work.)"
)


def run_life_review_once(now: datetime | None = None) -> dict:
    """A periodic re-read of the household's data as a life, marker-gated like
    the check-in: the marker only advances on success, so a failed review is
    retried next tick instead of skipped for a week."""
    if not config.LIFE_REVIEW_DAYS:
        return {"status": "disabled"}
    now = now or datetime.now()
    with session_scope() as session:
        note = session.execute(
            select(MemoryNote).where(MemoryNote.title == LIFE_REVIEW_MARKER_TITLE)
        ).scalar_one_or_none()
        if note is not None:
            try:
                last = date.fromisoformat(note.content.strip())
                if (now.date() - last).days < config.LIFE_REVIEW_DAYS:
                    return {"status": "not_due"}
            except ValueError:
                pass
        history = chat_thread.build_history(session)
    history.append({"role": "user", "content": LIFE_REVIEW_PROMPT})
    with session_scope() as session:
        reply = agent_chat.run_turn(session, history, channel="tending")
    with session_scope() as session:
        note = session.execute(
            select(MemoryNote).where(MemoryNote.title == LIFE_REVIEW_MARKER_TITLE)
        ).scalar_one_or_none()
        if note:
            note.content = now.date().isoformat()
        else:
            session.add(MemoryNote(
                title=LIFE_REVIEW_MARKER_TITLE, content=now.date().isoformat()
            ))
        if not agent_chat.is_silence(reply):
            session.add(ChatMessage(
                channel="web", role="assistant", speaker="copilot", content=reply
            ))
            return {"status": "spoke"}
    return {"status": "quiet"}


async def _life_review_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_life_review_once)
            if result["status"] in ("spoke", "quiet"):
                log.info("life review: %s", result["status"])
        except Exception:
            log.exception("life review loop error")
        await asyncio.sleep(6 * 3600)


def run_proposal_evaluations_once() -> dict:
    """Evaluate any un-tested code proposals in the sandbox. Only ever executes
    agent-authored tests inside the verified jail; a no-op until one is
    configured (proposals then simply wait for a trusted reviewer)."""
    from . import selfimprove_sandbox
    from .models import CodeProposal

    if not (config.SELFIMPROVE_EVAL_ENABLED and selfimprove_sandbox.configured()):
        return {"status": "no_sandbox"}
    evaluated = 0
    with session_scope() as session:
        pending = list(session.execute(
            select(CodeProposal).where(CodeProposal.status == "proposed")
        ).scalars())
        ids = [p.id for p in pending]
    for pid in ids:
        with session_scope() as session:
            selfimprove_sandbox.evaluate(session, pid)
        evaluated += 1
    return {"status": "ok", "evaluated": evaluated}


async def _selfimprove_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_proposal_evaluations_once)
            if result.get("evaluated"):
                log.info("proposal evaluation: %s", result)
        except Exception:
            log.exception("self-improve eval loop error")
        await asyncio.sleep(120)


WEEKLY_REPORT_MARKER_TITLE = "Last weekly report"

WEEKLY_NARRATIVE_PROMPT = (
    "(Saturday-morning printed report — this goes on paper, on the fridge. Below "
    "is this week's data, already computed. Write the 'From your copilot' section: "
    "a warm, plain-language summary of how the household did this week versus last "
    "week and the month, the one thing that most deserves attention, and how the "
    "road ahead looks if nothing changes. 4-8 sentences, plain text only — no "
    "markdown, no bullet points, no tool calls needed. Data:\n{data})"
)


def weekly_report_action(session, now: datetime) -> str:
    """'run' | 'skip'. Fires once per report day, on/after the report hour.
    No init pattern: if it IS Saturday morning, the household wants the page —
    a deploy that morning should print, not wait a week."""
    if not config.WEEKLY_REPORT:
        return "skip"
    if now.weekday() != config.WEEKLY_REPORT_WEEKDAY or now.hour < config.WEEKLY_REPORT_HOUR:
        return "skip"
    note = session.execute(
        select(MemoryNote).where(MemoryNote.title == WEEKLY_REPORT_MARKER_TITLE)
    ).scalar_one_or_none()
    if note is None:
        return "run"
    try:
        last = date.fromisoformat(note.content.strip())
    except ValueError:
        return "run"
    return "skip" if last >= now.date() else "run"


def _set_weekly_marker(session, today: date) -> None:
    note = session.execute(
        select(MemoryNote).where(MemoryNote.title == WEEKLY_REPORT_MARKER_TITLE)
    ).scalar_one_or_none()
    if note:
        note.content = today.isoformat()
    else:
        session.add(MemoryNote(title=WEEKLY_REPORT_MARKER_TITLE, content=today.isoformat()))


def run_weekly_report_once(now: datetime | None = None) -> dict:
    """Assemble, narrate, render, print, and deliver the weekly page.

    Delivery is layered so a dead printer never eats the report: the numbers
    and narrative always reach the household (email when configured, thread
    otherwise), and the print outcome is reported honestly either way. The
    marker is set after delivery, so a crash mid-flow retries next tick."""
    import json as _json

    from . import reports

    now = now or datetime.now()
    with session_scope() as session:
        if weekly_report_action(session, now) != "run":
            return {"status": "skip"}
        data = reports.gather_weekly_data(session, now.date())

    compact = {k: v for k, v in data.items() if k != "projection"}
    compact["projection_p50_5y"] = (
        (data.get("projection") or {}).get("bands") or [{}]
    )[-1].get("p50")
    try:
        with session_scope() as session:
            history = chat_thread.build_history(session)
            history.append({
                "role": "user",
                "content": WEEKLY_NARRATIVE_PROMPT.format(data=_json.dumps(compact)),
            })
            # The Saturday report is a complex synthesis — always Fable at max,
            # never downshifted by the router (Ford's ask 2026-08-11).
            narrative = agent_chat.run_turn(
                session, history, channel="web", force_tier="complex"
            )
        if agent_chat.is_silence(narrative):
            narrative = ""
    except Exception:
        log.exception("weekly report: narrative turn failed; printing numbers only")
        narrative = ""

    pdf_path = reports.REPORTS_DIR / f"weekly-{data['date']}.pdf"
    reports.render_pdf(data, narrative, pdf_path)

    printed, print_note = True, ""
    try:
        job = reports.print_pdf(pdf_path)
        log.info("weekly report printed: %s", job)
    except Exception as exc:
        printed, print_note = False, (
            f"\n\n(The printer was unreachable this morning — {exc}. "
            "The report is saved; ask me to print it again once the printer is on.)"
        )
        log.warning("weekly report: print failed: %s", exc)

    nw = (data.get("net_worth") or {}).get("total")
    body = (
        f"Weekly report for {data['date']}. Net worth: ${nw:,.0f}.\n\n{narrative}"
        f"{print_note}"
    ) if isinstance(nw, (int, float)) else f"Weekly report for {data['date']}.\n\n{narrative}{print_note}"

    from .messaging import email_thread

    with session_scope() as session:
        if email_thread.configured():
            email_thread.start_thread(
                session, f"Weekly household report — {data['date']}", body
            )
        else:
            session.add(ChatMessage(
                channel="web", role="assistant", speaker="copilot", content=body
            ))
    with session_scope() as session:
        _set_weekly_marker(session, now.date())
    return {"status": "ran", "printed": printed, "pdf": str(pdf_path)}


async def _weekly_report_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(run_weekly_report_once)
            if result["status"] == "ran":
                log.info("weekly report: printed=%s", result["printed"])
        except Exception:
            log.exception("weekly report loop error")
        await asyncio.sleep(30 * 60)


def run_sentinel_sweep() -> dict:
    """One Sentinel sweep in its own session (posture self-audit + ledger
    integrity + threat watch). Detects and alarms; never changes controls."""
    with session_scope() as session:
        return sentinel.run_sentinel_once(session)


async def _sentinel_loop() -> None:
    await asyncio.sleep(30)  # a first sweep shortly after boot
    while True:
        if config.SENTINEL_ENABLED:
            try:
                result = await asyncio.to_thread(run_sentinel_sweep)
                log.info("sentinel sweep: %s", result)
            except Exception:
                log.exception("sentinel sweep error")
        await asyncio.sleep(config.SENTINEL_INTERVAL_MINUTES * 60)


def start_background_tasks() -> list[asyncio.Task]:
    return [
        asyncio.create_task(_sentinel_loop(), name="bankai-sentinel"),
        asyncio.create_task(_sync_loop(), name="bankai-sync"),
        asyncio.create_task(_rules_loop(), name="bankai-rules"),
        asyncio.create_task(_realestate_loop(), name="bankai-realestate"),
        asyncio.create_task(_email_loop(), name="bankai-email"),
        asyncio.create_task(_email_chat_loop(), name="bankai-email-chat"),
        asyncio.create_task(_whatsapp_loop(), name="bankai-whatsapp-chat"),
        asyncio.create_task(_monthly_review_loop(), name="bankai-monthly-review"),
        asyncio.create_task(_tending_loop(), name="bankai-tending"),
        asyncio.create_task(_checkin_loop(), name="bankai-checkin"),
        asyncio.create_task(_life_review_loop(), name="bankai-life-review"),
        asyncio.create_task(_selfimprove_loop(), name="bankai-selfimprove"),
        asyncio.create_task(_weekly_report_loop(), name="bankai-weekly-report"),
    ]
