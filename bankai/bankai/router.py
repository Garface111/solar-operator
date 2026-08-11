"""Pick the model and reasoning effort to match each request.

Running Fable-5-at-max on every "what's my balance?" is why replies were slow.
This routes each turn to a tier instead:

* QUICK  — the household explicitly asked for speed ("real quick", "tldr", "one
           line"). Sonnet 5 at low effort. Their stated urgency wins even over a
           complex question — if they wanted the deep version they wouldn't say
           "real quick", and they can always ask for more.
* COMPLEX— analysis, planning, advice, anything consequential or long. Fable 5
           at max effort, and it keeps the adversarial verify pass, because this
           is exactly where a wrong number costs something.
* FAST    — everything else: lookups, confirmations, short factual asks. Opus 5
           at low effort, no verify — a balance readback needs speed, not a
           second opinion.

The classifier is pure string work, so it adds no latency of its own, and every
tier's model/effort is configurable. When routing is off, the turn falls back to
the fixed CLAUDE_CLI_MODEL / CLAUDE_CLI_EFFORT.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config

# --- explicit urgency: the household asking for speed, in their own words ---
_URGENT_RE = re.compile(
    r"\b(real\s*quick|quick\s*q(?:uestion)?|quickly|be\s+quick|real\s+fast|"
    r"fast\s+answer|quick\s+answer|quick\s+take|asap|briefly|tl;?dr|"
    r"one[\s-]?(?:line|liner|word|sentence)|short\s+answer|"
    r"just\s+(?:tell|give)\s+me|keep\s+it\s+(?:short|brief)|"
    r"no\s+(?:detail|essay)|don'?t\s+overthink)\b",
    re.IGNORECASE,
)
# bare "quick" as a whole word (not "quicken", "quickbooks") also counts
_BARE_QUICK_RE = re.compile(r"\bquick\b", re.IGNORECASE)

# --- complexity: depth is warranted, and a wrong number would cost something ---
_COMPLEX_RE = re.compile(
    r"\b(analy[sz]e|analysis|plan|planning|strateg|forecast|projection|project\s+(?:our|my|out)|"
    r"model\s+out|compare|comparison|versus|vs\.?|should\s+(?:we|i)|worth\s+it|"
    r"break\s+(?:it\s+)?down|walk\s+me\s+through|deep\s+dive|thorough|comprehensive|"
    r"evaluate|assess|trade[\s-]?off|scenario|what\s+if|optimi[sz]e|restructure|"
    r"refinance|afford|retirement|decades|years\s+out|\d+\s*years?|contract|lease|"
    r"legal|estate|trust\s+agreement|payoff|pay\s+off|"
    r"debt\s+plan|budget\s+plan|allocate|rebalance|recommend|cancel|transfer)\b",
    re.IGNORECASE,
)

# Long asks tend to be complex even without a keyword.
COMPLEX_LENGTH_CHARS = 320


@dataclass
class RouteDecision:
    tier: str      # quick | fast | complex | default
    model: str
    effort: str
    verify: bool
    reason: str


def _current_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _complex(reason: str) -> RouteDecision:
    return RouteDecision(
        "complex", config.ROUTER_COMPLEX_MODEL, config.ROUTER_COMPLEX_EFFORT,
        verify=True, reason=reason,
    )


def _is_complex(text: str) -> bool:
    if len(text) > COMPLEX_LENGTH_CHARS:
        return True
    if text.count("?") >= 2:
        return True
    return bool(_COMPLEX_RE.search(text))


def for_tier(tier: str) -> RouteDecision:
    """A decision for an explicitly named tier — used when a caller KNOWS the
    work is complex (the Saturday report, a monthly review) and wants Fable-max
    regardless of how the prompt happens to read."""
    tier = (tier or "").strip().lower()
    if tier == "quick":
        return RouteDecision("quick", config.ROUTER_QUICK_MODEL,
                             config.ROUTER_QUICK_EFFORT, verify=False, reason="forced quick")
    if tier == "fast":
        return RouteDecision("fast", config.ROUTER_FAST_MODEL,
                             config.ROUTER_FAST_EFFORT, verify=False, reason="forced fast")
    return _complex("forced complex")


def choose(messages: list[dict], channel: str = "web") -> RouteDecision:
    """The model/effort/verify for this turn."""
    if not config.ROUTER_ENABLED:
        return RouteDecision(
            "default", config.CLAUDE_CLI_MODEL, config.CLAUDE_CLI_EFFORT,
            verify=(channel != "sms"), reason="router disabled",
        )

    # Self-directed background work: nobody is waiting, so favor depth.
    if channel == "tending":
        return _complex("background turn")

    text = _current_user_text(messages)

    # Explicit urgency wins outright — respect the human's stated speed choice.
    if _URGENT_RE.search(text) or _BARE_QUICK_RE.search(text):
        return RouteDecision(
            "quick", config.ROUTER_QUICK_MODEL, config.ROUTER_QUICK_EFFORT,
            verify=False, reason="explicit urgency",
        )

    if _is_complex(text):
        return _complex("complex/consequential request")

    return RouteDecision(
        "fast", config.ROUTER_FAST_MODEL, config.ROUTER_FAST_EFFORT,
        verify=False, reason="simple request",
    )
