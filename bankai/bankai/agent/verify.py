"""The adversarial conscience — a second model pass that attacks a reply
before the household ever sees it.

A fiduciary that hallucinates a number is worse than no fiduciary at all. So
before a *consequential* reply leaves the agent, a critic pass re-reads the
conversation (including any tool data quoted in it) and tries to break the
reply: are the dollar figures internally consistent and grounded in the tool
results? Is the recommendation overconfident? Is a caveat missing? Is the
arithmetic right? If the critic finds a material problem, the same backend is
asked to rewrite the reply once, fixing only what was flagged.

Design rules, in priority order:

1. **Never break the main flow.** Every failure path — a backend that raises,
   a critic that returns garbage, a revision that comes back empty — degrades
   silently to the original reply. The verifier is a safety net, not a
   dependency.
2. **Cheap gate first.** `is_consequential()` is pure string work. Small talk
   and one-line lookups skip verification entirely, so the attention and cost
   budget is spent only where a wrong number could actually hurt.
3. **Backend agnostic.** `run_backend` has the same signature as every backend
   `run()` in `bankai.agent.backends` — `(session, system, messages) -> str` —
   so the caller passes whichever impl won the fallback chain and the verifier
   stays out of backend selection.

The on/off switch is `bankai.config.VERIFY_REPLIES` (env `VERIFY_REPLIES`,
default true). It is mirrored into a module-level constant here and read at call
time inside `verified_turn`, so tests (and an operator at a REPL) can flip
`verify.VERIFY_REPLIES` directly.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from .. import config

log = logging.getLogger("bankai.verify")

# A backend `run()`: (session, system, messages) -> reply text.
RunBackend = Callable[[Any, str, list[dict]], str]

VERIFY_REPLIES: bool = config.VERIFY_REPLIES

# Money below this never trips the gate — a $4 coffee charge is not worth a
# second model pass.
CONSEQUENTIAL_MIN_DOLLARS = 100.0

# How much of the conversation the critic sees. Enough for it to check the
# reply against quoted tool output without re-sending an entire thread.
CRITIC_HISTORY_MESSAGES = 8
CRITIC_MESSAGE_CHARS = 4000


# --- the cheap deterministic gate ---

_MONEY_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%|\b\d+(?:\.\d+)?\s*percent\b", re.IGNORECASE)
_ADVICE_RE = re.compile(
    r"\b(afford|affordable|recommend|recommends|recommended|recommendation|"
    r"should|shouldn't|cancel|cancels|cancelling|canceled|cancelled|"
    r"transfer|transfers|transferring|transferred)\b",
    re.IGNORECASE,
)
_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"  # 2026-07-30
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"  # 7/30 or 7/30/2026
    rf"|\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"  # July 30 / Jul 30th
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b",  # 30 July
    re.IGNORECASE,
)


def _max_dollar_amount(reply: str) -> float:
    """Largest dollar figure in `reply`, or 0.0 when there is none."""
    largest = 0.0
    for raw in _MONEY_RE.findall(reply):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:  # pragma: no cover - regex already constrains this
            continue
        largest = max(largest, value)
    return largest


def is_consequential(reply: str) -> bool:
    """True when a wrong number or an overconfident claim in `reply` could cost
    the household something.

    Deliberately cheap and deliberately over-inclusive: this runs on every turn,
    so it is pure regex work, and a false positive only costs one extra model
    call while a false negative ships an unchecked number.
    """
    if not reply or not reply.strip():
        return False
    if _max_dollar_amount(reply) >= CONSEQUENTIAL_MIN_DOLLARS:
        return True
    if _PERCENT_RE.search(reply):
        return True
    if _ADVICE_RE.search(reply):
        return True
    if _DATE_RE.search(reply):
        return True
    return False


# --- the critic ---

CRITIC_SYSTEM = """You are the adversarial conscience of a household finance copilot.
Another model just drafted the reply below. Your job is to ATTACK it, not to praise it.

EPISTEMIC LIMITS — read this first: the drafting model had live tools over the
household's real database (balances, transactions, projections) and its tool results
are NOT visible to you. A specific dollar figure you cannot see the source of is most
likely grounded in tool data you were not shown. Do NOT flag a number merely because
its source is not visible in the conversation — that is your blindness, not the
draft's hallucination.

What you CAN and MUST attack:

1. INTERNAL CONSISTENCY. Numbers in the reply that contradict each other or contradict
   figures quoted earlier in the visible conversation: a total that does not match its
   parts, a percentage that does not follow from its own numerator and denominator, a
   date that conflicts with another date in the same reply.
2. ARITHMETIC. Re-do every sum, difference, average, percentage, and payment
   calculation using ONLY figures stated in the reply itself. Flag results that do not
   check out from the reply's own numbers.
3. IMPOSSIBLE OR ABSURD VALUES. Figures that cannot be right on their face (a mortgage
   payment larger than the loan, a negative count, a date in the past for a future
   event).
4. OVERCONFIDENCE. Predictions or judgments stated as certainties — "you can afford
   this", "you'll have $X left" — without confidence framing, when the reply itself
   admits (or fails to mention) that data is thin, stale, or partial.
5. MISSING CAVEATS. Consequential recommendations (cancelling, transferring, signing,
   large purchases) that omit an obvious risk, an unstated assumption, or that a
   licensed professional should confirm before acting.

Do NOT flag: tone, wording, formatting, length, numbers whose source is simply not
visible to you, or anything you merely would have phrased differently. Only material
problems that change what the household would believe or do.

Respond with JSON ONLY — no prose before or after, no markdown fences:
{"verdict": "pass" | "revise", "problems": ["...", "..."], "severity": "low" | "high"}

Use "revise" only when you found a material problem, and then list each problem as one
concrete sentence naming the specific number or claim. Use "pass" with an empty problems
list when the reply holds up. Use severity "high" when a problem could lead the household
to act on a wrong number."""

REVISION_SYSTEM = """You are revising your own previous reply to a household finance
question. A verification pass found specific problems with it.

Fix ONLY the identified problems. Keep everything else — the structure, the tone, the
correct figures, the parts that were not flagged — exactly as it was. Do not add new
analysis, do not expand the scope, do not apologize or mention that a revision happened.
If a number cannot be supported by the data in the conversation, remove it or say plainly
that the data does not support it rather than substituting another guess.

Output the corrected reply text and nothing else."""


def _render_history(messages: list[dict]) -> str:
    """Flatten recent conversation turns into plain text for the critic."""
    lines: list[str] = []
    for message in list(messages)[-CRITIC_HISTORY_MESSAGES:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        who = "User" if role == "user" else "Copilot"
        lines.append(f"{who}: {content[:CRITIC_MESSAGE_CHARS]}")
    return "\n\n".join(lines)


def _extract_json_object(text: str) -> dict | None:
    """Find and parse the first balanced ``{...}`` block in `text`.

    Models wrap JSON in prose, markdown fences, or a preamble no matter how the
    prompt is worded, so scanning for a brace-balanced object is more reliable
    than parsing the whole string. Returns None when nothing parses.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except (ValueError, TypeError):
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def _normalize_problems(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        problems = []
        for item in raw:
            text = item if isinstance(item, str) else str(item)
            text = text.strip()
            if text:
                problems.append(text)
        return problems
    return []


def _passed(note: str) -> dict:
    """A pass verdict carrying why the critic did not produce one itself."""
    return {"verdict": "pass", "problems": [], "severity": "low", "note": note}


def critique(
    session: Any,
    original_messages: list[dict],
    reply: str,
    run_backend: RunBackend,
) -> dict:
    """Run one critic pass over `reply`.

    `run_backend` has the same signature as the backend `run()` functions —
    ``(session, system, messages) -> str``.

    Returns ``{"verdict": "pass"|"revise", "problems": [...], "severity":
    "low"|"high"}``, plus a ``note`` key when the critic could not be reached or
    its output could not be parsed. Parsing is defensive on purpose: an
    unreadable critic must never be able to block or corrupt a reply, so every
    failure resolves to "pass".
    """
    history = _render_history(original_messages or [])
    critic_prompt = (
        "Conversation so far (this is the only data the copilot had — any number "
        "in the reply that does not appear here or follow arithmetically from it "
        "is unsupported):\n\n"
        f"{history or '(no prior conversation)'}\n\n"
        "--- DRAFT REPLY UNDER REVIEW ---\n"
        f"{reply}\n"
        "--- END DRAFT REPLY ---\n\n"
        "Attack this draft now. Respond with the JSON object only."
    )
    critic_messages = [{"role": "user", "content": critic_prompt}]

    try:
        raw = run_backend(session, CRITIC_SYSTEM, critic_messages)
    except Exception as exc:  # the verifier must never break the main flow
        log.warning("critic backend failed, passing reply through: %s", exc)
        return _passed(f"critic backend failed: {exc}")

    if not isinstance(raw, str) or not raw.strip():
        return _passed("critic returned no output")

    parsed = _extract_json_object(raw)
    if parsed is None:
        log.warning("critic output was not parseable JSON, passing reply through")
        return _passed("critic output was not parseable JSON")

    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "revise"):
        return _passed(f"critic returned unknown verdict {verdict!r}")

    severity = str(parsed.get("severity", "low")).strip().lower()
    if severity not in ("low", "high"):
        severity = "low"

    problems = _normalize_problems(parsed.get("problems"))
    if verdict == "revise" and not problems:
        # "revise" with nothing to fix is not actionable — treat it as a pass
        # rather than burning a revision call on an empty problem list.
        return _passed("critic said revise but listed no problems")

    return {"verdict": verdict, "problems": problems, "severity": severity}


def _revise(
    session: Any,
    messages: list[dict],
    reply: str,
    problems: list[str],
    run_backend: RunBackend,
) -> str | None:
    """Ask the same backend to fix only `problems`. None on any failure."""
    numbered = "\n".join(f"{i}. {p}" for i, p in enumerate(problems, start=1))
    history = _render_history(messages or [])
    revision_prompt = (
        "Conversation so far:\n\n"
        f"{history or '(no prior conversation)'}\n\n"
        "--- YOUR PREVIOUS REPLY ---\n"
        f"{reply}\n"
        "--- END PREVIOUS REPLY ---\n\n"
        "A verification pass found these problems with it:\n"
        f"{numbered}\n\n"
        "Rewrite the reply fixing ONLY these problems. Keep everything else the "
        "same. Output the corrected reply text and nothing else."
    )
    try:
        revised = run_backend(
            session, REVISION_SYSTEM, [{"role": "user", "content": revision_prompt}]
        )
    except Exception as exc:  # degrade to the original reply
        log.warning("revision backend failed, keeping original reply: %s", exc)
        return None
    if not isinstance(revised, str) or not revised.strip():
        log.warning("revision returned no text, keeping original reply")
        return None
    return revised.strip()


def verified_turn(
    session: Any,
    messages: list[dict],
    reply: str,
    run_backend: RunBackend,
    max_revisions: int = 1,
) -> tuple[str, dict]:
    """Gate, critique, and (at most `max_revisions` times) revise `reply`.

    Returns ``(final_reply, report)`` where report carries::

        {"verified": bool, "verdict": str, "problems": list[str], "revised": bool}

    `verified` is False when the pass was skipped (disabled by config, or the
    reply was not consequential). Every failure — an unreachable backend, an
    unparseable critic, an empty revision — returns the ORIGINAL reply, so the
    worst case of this function is the behaviour you had without it.
    """
    report: dict = {
        "verified": False,
        "verdict": "skipped",
        "problems": [],
        "revised": False,
    }

    if not VERIFY_REPLIES:
        report["reason"] = "disabled"
        return reply, report

    try:
        if not is_consequential(reply):
            report["reason"] = "not_consequential"
            return reply, report

        result = critique(session, messages, reply, run_backend)
        report["verified"] = True
        report["verdict"] = result.get("verdict", "pass")
        report["problems"] = result.get("problems", [])
        report["severity"] = result.get("severity", "low")
        if "note" in result:
            report["note"] = result["note"]

        if report["verdict"] != "revise" or max_revisions < 1:
            return reply, report

        revised = _revise(session, messages, reply, report["problems"], run_backend)
        if revised is None:
            report["reason"] = "revision_failed"
            return reply, report

        report["revised"] = True
        return revised, report
    except Exception as exc:  # belt and braces: nothing here may reach the user
        log.warning("verification failed, returning original reply: %s", exc)
        report["reason"] = f"verification_error: {exc}"
        return reply, report
