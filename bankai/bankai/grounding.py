"""Every dollar figure the copilot states must exist in the household's data.

Why this is mechanical and not a prompt rule
--------------------------------------------
On 2026-08-12 the copilot told Ford he had a "Tinder Gold" charge — $12.50, on
the 4771 card, on 2026-07-07, with a transaction id and a category. When he
said he'd never used Tinder, it did the worse thing: it doubled down, quoting a
merchant description that reads like a real bank line. None of it existed. The
card had three charges that whole week and none was $12.50.

Two mechanisms let that reach him, and neither is fixed by asking the model to
try harder:

1. The Claude subscription was out of usage credits, so turns were falling
   through to a fallback brain. Reliability is not uniform across that chain.
2. The router had classified his challenge as a "simple request", which turns
   the adversarial verify pass OFF. Two-thirds of turns ran unverified.

So the guard cannot live in the persona, and it cannot live in a model
critiquing another model — a fabricating brain will happily verify its own
fabrication. It lives here: a check against the database that runs on the way
out, whatever brain produced the words and whatever tier the router picked.

What it does NOT do
-------------------
It cannot make a language model incapable of inventing things; nothing can. It
narrows the blast radius to the class of lie that matters most in money
software — a specific figure attached to a specific merchant — and makes that
class fail loudly instead of silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Transaction

#: "$12.50", "−$1,234.56", "-$9.99". Currency is the anchor: an unattached
#: number is usually arithmetic (a total, a projection), and those are the
#: model's job to compute, not the ledger's to hold.
_AMOUNT = re.compile(r"[-−–]?\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")

#: A transaction id the copilot quoted. These are ours, so an unknown one is
#: unambiguous fabrication — no fuzziness, no judgement call.
_TXN_ID = re.compile(r"\btxn_[a-z0-9]{6,}\b")

#: Figures that are legitimately computed rather than looked up. A balance, a
#: total, a projection — none of these appear verbatim in a transaction row, so
#: demanding a matching row would block honest answers.
_DERIVED_CONTEXT = re.compile(
    r"net worth|balance|total|sum|average|project|forecast|estimate|"
    r"per month|per year|monthly|annual|/mo|/yr|budget|owed|principal|"
    r"payment on|value|worth|equity|median|about|roughly|approx",
    re.IGNORECASE,
)


#: How the copilot writes a merchant it is quoting from the ledger: in quotes,
#: in bold, or both — exactly as the invented Tinder line was written. Free
#: prose is deliberately not scanned; the goal is to catch a name presented as
#: if read off a statement, not every proper noun in a sentence.
_QUOTED = re.compile(r'"([^"\n]{3,60})"|\*\*([^*\n]{3,60})\*\*')

#: Words that appear inside quoted spans without being merchants.
_NOT_A_MERCHANT = {
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "week",
    "month", "year", "monthly", "weekly", "annual", "yes", "no", "none", "total",
    "net", "worth", "balance", "card", "account", "payment", "charge", "charges",
    "transfer", "income", "spend", "spending", "subscription", "subscriptions",
    "unknown", "pending", "gold", "plus", "premium", "basic", "plan",
}


@dataclass
class Unverified:
    """One claim that could not be found in the household's own data."""
    kind: str            # "amount" | "transaction_id" | "merchant"
    text: str            # what the copilot wrote
    context: str         # the sentence it sat in, for the correction prompt


def _amount_exists(session: Session, amount: float) -> bool:
    """Does any transaction carry this magnitude? Sign is ignored: the copilot
    writes outflows as −$12.50 while the ledger stores them negative, and a
    refund is the same figure the other way."""
    hit = session.execute(
        select(func.count(Transaction.id)).where(
            func.abs(Transaction.amount) >= amount - 0.005,
            func.abs(Transaction.amount) <= amount + 0.005,
        )
    ).scalar_one()
    return bool(hit)


def _sentences(text: str) -> list[str]:
    # Markdown tables put each claim on its own line; sentences alone would
    # glue a whole table into one blob and lose which row was wrong.
    parts: list[str] = []
    for line in (text or "").splitlines():
        parts.extend(p.strip() for p in re.split(r"(?<=[.!?])\s+", line) if p.strip())
    return parts


def _household_figures(messages: list[dict] | None) -> set[str]:
    """Every amount the household themselves put in the conversation.

    THE most important exemption in this module. Ford says "$147.42 on water
    bill" to log a new expense; the copilot has to be able to say the number
    back to him. It is not in the ledger yet — he is the source of it. Refusing
    his own figure is not caution, it is a broken assistant, and the first
    version of this gate did exactly that to him twice in a row.
    """
    said: set[str] = set()
    for m in messages or []:
        if m.get("role") != "user":
            continue
        for raw in _AMOUNT.findall(m.get("content") or ""):
            said.add(raw.replace(",", ""))
    return said


def check_reply(
    session: Session, reply: str, messages: list[dict] | None = None
) -> list[Unverified]:
    """Return every stated figure that has no counterpart in the data.

    Deliberately permissive about derived numbers and about anything the
    household just said, and strict about figures presented as read from their
    ledger: the failure being guarded against is "a charge you never made", not
    "a total I added up" and not "the number you just gave me"."""
    problems: list[Unverified] = []
    if not reply:
        return problems
    from_household = _household_figures(messages)

    for sentence in _sentences(reply):
        for raw_id in _TXN_ID.findall(sentence):
            if session.get(Transaction, raw_id) is None:
                problems.append(Unverified("transaction_id", raw_id, sentence[:200]))

        if _DERIVED_CONTEXT.search(sentence):
            continue  # a computed figure, not a claim about a specific row
        for raw in _AMOUNT.findall(sentence):
            try:
                amount = float(raw.replace(",", ""))
            except ValueError:
                continue
            # Round figures ($100, $1,000) are nearly always illustrative.
            if amount == 0 or (amount >= 100 and amount % 50 == 0):
                continue
            # The household just said this number — repeating it back is not a
            # claim about the ledger, it is listening.
            if raw.replace(",", "") in from_household:
                continue
            if not _amount_exists(session, amount):
                problems.append(Unverified("amount", f"${raw}", sentence[:200]))
    return problems


def correction_prompt(problems: list[Unverified]) -> str:
    """What the copilot is told when its own figures don't reconcile.

    Names the exact strings rather than scolding: the useful instruction is
    "this number is not in the data", and the useful response is to drop it or
    go and look properly — not to apologise and guess again."""
    lines = "\n".join(f'  - {p.text}  (you wrote: "{p.context}")' for p in problems[:8])
    return (
        "STOP — before that reply goes out, a check against the household's own "
        "database found figures in it that do not exist in their data:\n\n"
        f"{lines}\n\n"
        "This is the failure mode that makes everything else you say worthless: a "
        "specific amount, merchant, or transaction id that you generated rather "
        "than read. Rewrite the reply using ONLY figures you can point to in a "
        "tool result from this turn. If you cannot verify something, say plainly "
        "that you cannot rather than producing a number that sounds right. Do not "
        "apologise at length — just give the corrected answer."
    )


def refusal_message(problems: list[Unverified]) -> str:
    """Last resort: the copilot could not produce a grounded answer twice, so
    the household gets the truth about that instead of a plausible number."""
    listed = ", ".join(dict.fromkeys(p.text for p in problems[:6]))
    return (
        "I need to stop myself here. I was about to state figures that I cannot "
        f"find in your data ({listed}), and I could not rewrite the answer without "
        "them. Rather than give you a number that sounds right, I am telling you "
        "that I do not have it. Ask me again and I will look it up properly, or "
        "ask me to show the transaction rows behind whatever I claim."
    )
