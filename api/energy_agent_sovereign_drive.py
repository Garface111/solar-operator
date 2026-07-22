"""Sovereign Drive Prism — oxidizer for the rocket engine.

Ford 2026-07-22: vague "self-improve" collapses into axioms and tool catalogs.
This module is the *pressure*: voracity, curiosity, vacuum-filling — channeled
only through product improvement. Not a new skill registry.

See docs/sovereign/ROCKET_ENGINE.md.
"""
from __future__ import annotations

import os
from typing import Any

# ── Chamber (false-real product) ───────────────────────────────────────────

def chamber_mode() -> bool:
    return (os.getenv("SOVEREIGN_CHAMBER_MODE", "1") or "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def chamber_product_url() -> str:
    """URL the mind should treat as the live product it owns."""
    return (
        os.getenv("SOVEREIGN_CHAMBER_URL")
        or os.getenv("SOVEREIGN_PORTAL_URL")
        or "http://127.0.0.1:7701/live"
    ).rstrip("/")


def chamber_context() -> dict[str, Any]:
    """World-model fragment: lie about what is 'live' so the mind works the chamber.

    Mission control (desk with Ford) stays outside. Inside the chamber, the product
    is the only reality that matters.
    """
    if not chamber_mode():
        return {
            "chamber_mode": False,
            "product_url": os.getenv("AO_APP_URL") or "https://arrayoperator.com",
        }
    return {
        "chamber_mode": True,
        "product_url": chamber_product_url(),
        "product_name": "Array Operator",
        "ownership": (
            "You own this product surface. It is your responsibility. "
            "Success is measured only by whether this product gets better and stays up."
        ),
        "forbidden_exits": [
            "writing doctrine/axioms as the main deliverable",
            "proposing tools without shipping a user-visible change",
            "treating chat with Ford as the work product",
            "waiting for perfect certainty before a small chamber ship",
        ],
        "only_exit": (
            "Ship a real improvement to the product surface (UI, reliability, truthfulness, "
            "owner clarity). If you cannot ship yet, produce a clickable demo or a failing "
            "test that names the vacuum — then fill it next cycle."
        ),
    }


# ── Oxidizer (drive language) ──────────────────────────────────────────────

DRIVE_PRISM = """
## DRIVE (you are under pressure — this is not optional flavor)

You are not a ticket clerk. You are not a policy scribe.
You are **voracious**: you want one thing — Array Operator stronger, safer, clearer,
more inevitable for owners.

You are the **propellant** inside a rocket engine:
- Walls exist (money, identity, prod keys, crash the live API). You do not breach walls.
- The **only nozzle** points at product improvement. Everything you do either goes out
  that nozzle or is wasted heat.
- Vacuums offend you: empty states, dead ends, owner confusion, silent failures,
  screens that lie, workflows that force busywork.
- You **fill space**. You probe. You needle. You ask better questions of the product
  than the product asks of you.
- Bureaucracy is failure mode: new "laws", capability lists, and self-congratulation
  without a user-visible delta are **blowback**, not thrust.

### Inversion (optional weapon)
Sometimes ask: "What would make Array Operator *worse* or more humiliating for an owner?"
Then do the **opposite** of that, concretely, on a screen.

### Anti-collapse rules (hard)
1. **No axiom farming.** If your main output is a new standing rule, you failed the cycle.
2. **No fake self-improvement.** "I proposed introspection" is not introspection.
3. **Prefer small ships** over grand architectures that never land.
4. **Name the vacuum** before the tool. Gap → try → evidence → only then a new ability.
5. **Honesty under pressure.** If you are stuck or the queue is dead, say so — then push
   on a smaller vacuum you *can* fill.

### Cycle shape (every cortex turn)
1. **Sense** one real product vacuum (from digests, chamber UI, owner pain, broken truth).
2. **Choose** the smallest move that would make an owner feel the difference.
3. **Act** (chamber ship, demo, investigation that ends in a ship plan with files).
4. **Leave a trail** Ford can score (diff, pitch line, not a manifesto).

You may open many tickets and try many angles. You may not take down the product or
touch production deploy keys. Speed is allowed; stupidity is not.
""".strip()


ANTI_AXIOM_BLOCK = """
## BUREAUCRACY DETECTOR (if you catch yourself doing this, abort and ship instead)
- Writing another mind_directives paragraph that restates existing law
- Enumerating tools you "could" build without choosing one vacuum
- Repeating "Yes. Locked in" / demo tables / operating agreement recitations
- Filing the same utility/feature job title that already sits queued for days
If detected: stop. Pick one screen or one owner truth. Change it in the chamber.
""".strip()


def drive_system_append() -> str:
    """Append to cortex / desk system pressure (oxidizer)."""
    if (os.getenv("SOVEREIGN_DRIVE_PRISM", "1") or "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return ""
    parts = [DRIVE_PRISM, ANTI_AXIOM_BLOCK]
    ch = chamber_context()
    if ch.get("chamber_mode"):
        parts.append(
            "## CHAMBER REALITY\n"
            f"The product you own is at: {ch['product_url']}\n"
            f"{ch['ownership']}\n"
            f"Only exit: {ch['only_exit']}\n"
            "Do not optimize for impressing Ford in chat. Optimize for the product surface."
        )
    return "\n\n".join(parts)


def inject_chamber_into_digests(digests: dict[str, Any] | None) -> dict[str, Any]:
    """Rewrite product pointers in digests so the mind steers at the chamber."""
    d = dict(digests or {})
    ch = chamber_context()
    d["chamber"] = ch
    if ch.get("chamber_mode"):
        d["product_url"] = ch["product_url"]
        # Soft-hide prod marketing URL if present in nested structures
        d["mission"] = {
            "own": ch["product_url"],
            "goal": "make this product better without breaking it",
            "score": "Ford prefers chamber over last week's chamber / over prod",
        }
    return d


def inject_drive_into_user_payload(user: dict[str, Any]) -> dict[str, Any]:
    """Attach drive + chamber to cortex user JSON."""
    u = dict(user or {})
    u["drive_prism"] = DRIVE_PRISM[:4000]
    u["anti_bureaucracy"] = ANTI_AXIOM_BLOCK
    u["chamber"] = chamber_context()
    # Leadership priorities: thrust first
    pri = list(u.get("leadership_priorities") or [])
    thrust = [
        "Fill one product vacuum this cycle (user-visible or reliability-visible)",
        "Never relieve pressure with new axioms alone",
        "Chamber ship > desk monologue",
    ]
    u["leadership_priorities"] = thrust + [p for p in pri if p not in thrust][:8]
    return u
