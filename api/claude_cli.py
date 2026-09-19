"""Energy Agent brain over the Claude Code CLI — Ford's Claude subscription.

Ford asked (2026-09-18) for the Energy Agent to run on the Claude subscription
he already pays for instead of metered credits, after BOTH metered providers ran
dry on the same day: Anthropic returned "credit balance is too low" and xAI
returned "used all available credits or reached its monthly spending limit". A
subscription does not run out of credits mid-week, so it is the one backend that
cannot fail that particular way.

It can fail a DIFFERENT way, and the guards below exist for it. The subscription
is a PERSONAL seat that also carries Ford's own Claude Code sessions, Scribe's
07:00 newsroom and BankAI. Solar Operator is a multi-tenant product whose
background jobs alone made ~1,400-2,400 model calls a week through August. Left
ungoverned, this backend would race Ford for his own window — and a single stuck
agent loop would drain it (one real turn on 2026-09-18 fired four
`escalate_to_ford` calls in a row by feeding its own tool output back to itself).
So:

  * one call at a time, process-wide  (EA_CLAUDE_CLI_CONCURRENCY, default 1)
  * a hard per-day ceiling            (EA_CLAUDE_CLI_DAILY_MAX, default 300)
  * a circuit breaker: the moment the CLI reports a usage/session limit the
    backend goes cold for a cooldown instead of hammering the seat
    (EA_CLAUDE_CLI_COOLDOWN_S, default 1800)

Every guard FAILS OPEN INTO THE CHAIN: raising RuntimeError here just means
`_llm_call` moves to the next provider. The product degrades to another brain;
it never degrades into burning the subscription Ford's whole house runs on.

The CLI is an agent harness, not a raw messages endpoint, so tool use is carried
by an explicit JSON contract (see _SYSTEM_CONTRACT) rather than native
tool_calls. A reply that is not valid JSON is returned as plain prose instead of
raising — a chatty answer is a worse answer, never a broken turn.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("energy_agent.claude_cli")

# Linux caps ONE argv string at MAX_ARG_STRLEN (128 KiB) regardless of ARG_MAX;
# a long fleet context crosses it and execve refuses with E2BIG. Anything big
# travels down stdin instead. (Learned the hard way in Scribe, 2026-09-06.)
_ARGV_PROMPT_LIMIT = 120_000

_USAGE_ERROR = re.compile(
    r"usage limit|rate.?limit|out of credit|credits?|quota|exceeded|"
    r"too many requests|\b429\b|limit reached|upgrade your plan|overloaded|"
    r"at capacity|insufficient|session limit",
    re.IGNORECASE,
)

# `--tools ""` empties the built-in set outright. Denylisting them instead
# (--disallowedTools) leaves the tools VISIBLE to the model: presented with a
# tool catalog in the system prompt it reaches for Claude Code's own Read/Bash,
# is refused, and the turn dies on error_max_turns with stop_reason=tool_use.
# Verified live 2026-09-18. Belt and braces: the denylist stays as a second
# barrier in case a future CLI changes what "" means.
_NO_TOOLS = ""
_DISALLOWED_TOOLS = (
    "Bash Read Edit Write WebSearch WebFetch Glob Grep NotebookEdit Task TodoWrite"
)

_SYSTEM_CONTRACT = """
=== OUTPUT CONTRACT (STRICT) ===
Reply with ONE JSON object and nothing else. No prose outside it, no code fence.

  {"content": "<what to say to the owner>", "tool_calls": []}

To call tools, put them in tool_calls instead of describing them:

  {"content": "", "tool_calls": [{"name": "<tool>", "arguments": {}}]}

Rules:
  - You have NO tools of your own and no filesystem. Never attempt a real tool
    call -- the ONLY way to use a tool is to name it inside this JSON.
  - `arguments` is a JSON object matching that tool's schema. Never a string.
  - Only ever name a tool from the catalog above. Never invent one.
  - Do not repeat a tool call that already appears in the transcript with the
    same arguments — its result is already there; read it and move on.
  - If you have what you need, answer with content and an empty tool_calls.
""".strip()


def _bin() -> str:
    """Resolve the CLI. The native installer drops it in ~/.local/bin, which is
    NOT on the PATH uvicorn inherits under Railpack (verified in production
    2026-09-18: PATH is /app/.venv/bin:/mise/shims:/usr/... and nothing else),
    so a bare "claude" is not findable even when the binary is right there."""
    explicit = (os.getenv("EA_CLAUDE_CLI_BIN") or "").strip()
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".local", "bin", "claude"),
                 "/root/.local/bin/claude"):
        if os.path.exists(cand):
            return cand
    return "claude"


# The CLI prefers ANTHROPIC_API_KEY over CLAUDE_CODE_OAUTH_TOKEN when both are
# present. This service sets ANTHROPIC_API_KEY for _call_anthropic, so an
# inherited environment made every subscription call bill the METERED account
# instead -- which is out of credits, so the CLI answered "Credit balance is too
# low" and the breaker tripped on a subscription that was perfectly healthy.
# (Found in production 2026-09-18; the isolated `env -i` bench test passed only
# because it had never inherited the key.) Strip the metered credentials so the
# subscription token is the ONLY one on the table.
_STRIP_FROM_CHILD = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_FROM_CHILD}
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        # Nothing to authenticate with; say so plainly rather than let the CLI
        # wander into an interactive login prompt inside a container.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = ""
    home = os.path.expanduser("~")
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in (env.get("PATH") or ""):
        env["PATH"] = local_bin + os.pathsep + (env.get("PATH") or "")
    return env


def enabled() -> bool:
    """Armed only on purpose. Never a silent default — this spends Ford's seat."""
    return (os.getenv("EA_CLAUDE_CLI") or "0").strip().lower() in ("1", "true", "yes", "on")


def _models() -> list[str]:
    raw = (os.getenv("EA_CLAUDE_CLI_MODELS") or "claude-sonnet-5").strip()
    return [m.strip() for m in raw.split(",") if m.strip()]


# ── guards ──────────────────────────────────────────────────────────────────
_GATE = threading.BoundedSemaphore(
    max(1, int(os.getenv("EA_CLAUDE_CLI_CONCURRENCY", "1") or 1))
)
_GATE_WAIT_S = float(os.getenv("EA_CLAUDE_CLI_GATE_WAIT_S", "20") or 20)

_lock = threading.Lock()
_day: str = ""
_calls_today: int = 0
_cold_until: float = 0.0


def _daily_max() -> int:
    return max(0, int(os.getenv("EA_CLAUDE_CLI_DAILY_MAX", "300") or 300))


def _cooldown_s() -> float:
    return float(os.getenv("EA_CLAUDE_CLI_COOLDOWN_S", "1800") or 1800)


def _reserve_slot() -> None:
    """Count this call against today's ceiling, or refuse into the chain."""
    global _day, _calls_today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _lock:
        if today != _day:
            _day, _calls_today = today, 0
        if _cold_until > time.time():
            left = int(_cold_until - time.time())
            raise RuntimeError(
                f"claude-cli cold for {left}s (subscription reported a limit)"
            )
        cap = _daily_max()
        if cap and _calls_today >= cap:
            raise RuntimeError(
                f"claude-cli daily ceiling reached ({_calls_today}/{cap}) — "
                "protecting the subscription; the chain falls through"
            )
        _calls_today += 1


def _trip_breaker(why: str) -> None:
    global _cold_until
    with _lock:
        _cold_until = time.time() + _cooldown_s()
    log.error(
        "claude-cli breaker TRIPPED for %ds — the subscription reported a limit: %s",
        int(_cooldown_s()), why[:200],
    )
    try:
        from .notify import send_internal_alert
        send_internal_alert(
            "[EnergyAgent] Claude subscription hit a usage limit",
            "The Energy Agent's claude-cli backend was refused by the "
            f"subscription and is cold for {int(_cooldown_s())}s.\n\n"
            f"detail: {why[:500]}\n\n"
            "Turns are falling through to the next brain in the chain. This is "
            "the personal seat that also carries Ford's own sessions, Scribe "
            "and BankAI — if this repeats, move the product back to a metered "
            "key rather than starving the house.",
        )
    except Exception:
        log.warning("claude-cli: breaker alert failed", exc_info=True)


def status() -> dict:
    """Operator-visible state — so a cold breaker is never invisible."""
    with _lock:
        return {
            "enabled": enabled(),
            "bin": _bin(),
            "models": _models(),
            "calls_today": _calls_today if _day else 0,
            "daily_max": _daily_max(),
            "cold_for_s": max(0, int(_cold_until - time.time())),
        }


# ── message / tool rendering ────────────────────────────────────────────────
def _tool_catalog(tools: list) -> str:
    if not tools:
        return "No tools are available this turn. Answer directly."
    lines = ["=== TOOL CATALOG ==="]
    for t in tools:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        lines.append(f"- {name}: {desc[:400]}")
        lines.append(f"  schema: {json.dumps(params)[:900]}")
    return "\n".join(lines)


def _render(messages: list[dict]) -> tuple[str, str]:
    """Split into (system, transcript). The CLI takes one system + one prompt."""
    sys_parts: list[str] = []
    turns: list[str] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):  # multimodal → keep the text
            content = " ".join(
                (p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        content = "" if content is None else str(content)
        if role == "system":
            sys_parts.append(content)
        elif role == "tool":
            turns.append(
                f"[TOOL RESULT {m.get('name') or m.get('tool_call_id') or ''}]\n{content}"
            )
        elif role == "assistant" and m.get("tool_calls"):
            calls = [
                {
                    "name": (tc.get("function") or {}).get("name"),
                    "arguments": (tc.get("function") or {}).get("arguments"),
                }
                for tc in m["tool_calls"]
            ]
            body = json.dumps({"content": content, "tool_calls": calls})
            turns.append(f"[ASSISTANT]\n{body}")
        elif role == "assistant":
            turns.append(f"[ASSISTANT]\n{content}")
        else:
            turns.append(f"[OWNER]\n{content}")
    return "\n".join(sys_parts).strip(), "\n\n".join(turns).strip()


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a reply (fences tolerated)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break
        start = s.find("{", start + 1)
    return None


def _to_message(reply: str) -> dict:
    """Map the contract reply onto the OpenAI-ish message the chain expects."""
    obj = _extract_json(reply)
    if obj is None:
        # Not JSON. A chatty answer beats a broken turn.
        return {"role": "assistant", "content": reply.strip()}
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": str(obj.get("content") or "").strip(),
    }
    calls = []
    for c in obj.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or (c.get("function") or {}).get("name")
        if not name:
            continue
        args = c.get("arguments")
        if args is None:
            args = (c.get("function") or {}).get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        calls.append({
            "id": "cli_" + uuid.uuid4().hex[:16],
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    if calls:
        msg["tool_calls"] = calls
    return msg


# ── the call ────────────────────────────────────────────────────────────────
def _run_once(system: str, prompt: str, model: str | None, timeout: float) -> tuple[str, str]:
    """One CLI invocation. Returns (reply, error) — exactly one is non-empty."""
    def cmd(prompt_on_argv: bool) -> list[str]:
        c = [_bin(), "-p"]
        if prompt_on_argv:
            c.append(prompt)
        c += [
            "--output-format", "json",
            "--append-system-prompt", system,
            # All context is in the prompt, so the harness needs no tools of its
            # own. Without this it wanders into a tool loop and dies on
            # error_max_turns -- and in a container it would be reaching at the
            # product's own source tree.
            "--tools", _NO_TOOLS,
            "--disallowedTools", _DISALLOWED_TOOLS,
            "--max-turns", "1",
        ]
        if model:
            c += ["--model", model]
        return c

    on_argv = len(prompt.encode("utf-8", "surrogatepass")) <= _ARGV_PROMPT_LIMIT

    def run(prompt_on_argv: bool):
        return subprocess.run(
            cmd(prompt_on_argv),
            capture_output=True, text=True,
            # An argv prompt leaves stdin open; the CLI then waits on it and
            # warns "no stdin data received in 3s". Close it explicitly.
            input=(None if prompt_on_argv else prompt),
            stdin=(subprocess.DEVNULL if prompt_on_argv else None),
            timeout=timeout,
            cwd="/tmp",  # never let the harness sit in the product's source tree
            env=_child_env(),
        )

    try:
        try:
            proc = run(on_argv)
        except OSError as exc:
            if on_argv and getattr(exc, "errno", None) == errno.E2BIG:
                proc = run(False)
            else:
                raise
    except FileNotFoundError:
        return "", f"claude CLI '{_bin()}' not installed"
    except subprocess.TimeoutExpired:
        return "", f"claude CLI timed out after {int(timeout)}s"

    detail = (proc.stderr or "") + (proc.stdout or "")
    low = detail.lower()
    if "not logged in" in low or "/login" in low or "invalid api key" in low:
        return "", "claude CLI not authenticated (set CLAUDE_CODE_OAUTH_TOKEN)"
    if "credit balance" in low and not os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        # Be explicit: this is the metered account answering, not the seat.
        return "", ("claude CLI billed a metered Anthropic account (no "
                    "CLAUDE_CODE_OAUTH_TOKEN set) and it is out of credits")
    data = None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        pass
    if proc.returncode != 0 or (
        isinstance(data, dict) and (data.get("is_error") or data.get("api_error_status"))
    ):
        msg = (data.get("result") if isinstance(data, dict) else "") or detail
        return "", f"{model or 'default'}: {str(msg)[:300]}"
    reply = (data.get("result") if isinstance(data, dict) else proc.stdout) or ""
    return str(reply).strip(), ""


def ask_text(prompt: str, *, system: str | None = None,
             max_tokens: int | None = None) -> Optional[str]:
    """One prompt in, the assistant's text out — or None if the CLI can't serve it.

    The thin path for call sites that want an answer rather than a tool-calling
    turn (schema mapping, header detection, extraction). Never raises: a
    disabled backend, a tripped breaker, a busy gate or a bad reply all return
    None so the caller can fall back to its metered path without a try/except
    at every site.
    """
    if not enabled():
        return None
    try:
        res = call([{"role": "user", "content": prompt}], [],
                   max_tokens=max_tokens) or {}
    except Exception as e:  # noqa: BLE001
        log.info("claude_cli.ask_text unavailable: %s", e)
        return None
    msg = res.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        # Defensive: block-style content if the contract ever changes shape.
        content = "".join(b.get("text", "") for b in content
                          if isinstance(b, dict))
    text = (content or "").strip() if isinstance(content, str) else ""
    return text or None


def call(messages: list[dict], tools: list, *, max_tokens: int | None = None) -> dict:
    """Same contract as _call_anthropic: {"message", "usage", "provider"}."""
    if not enabled():
        raise RuntimeError("claude-cli backend not enabled (EA_CLAUDE_CLI=1)")
    _reserve_slot()

    system, transcript = _render(messages)
    system = "\n\n".join(
        p for p in (system, _tool_catalog(tools), _SYSTEM_CONTRACT) if p
    )
    timeout = float(os.getenv("EA_CLAUDE_CLI_TIMEOUT_S", "120") or 120)

    if not _GATE.acquire(timeout=_GATE_WAIT_S):
        raise RuntimeError(
            "claude-cli busy (one call at a time protects the subscription)"
        )
    try:
        last_err = ""
        for model in _models():
            reply, err = _run_once(system, transcript, model, timeout)
            if reply:
                return {
                    "message": _to_message(reply),
                    # The CLI bills the subscription, not per token -- report
                    # zeros rather than invent numbers the ledger would trust.
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                    "provider": "claude-cli",
                }
            last_err = err or last_err
            if _USAGE_ERROR.search(err or ""):
                _trip_breaker(err)
                break
        raise RuntimeError(f"claude-cli failed: {last_err or 'no reply'}")
    finally:
        _GATE.release()
