"""Actionability gate for feature suggestions (Ford 2026-07-17): stop filing
questions / chatter / control text as build tickets, keep filing real UI changes."""
from api.feature_suggestions import is_actionable_suggestion, _strip_suggestion_markup


# The real junk that clogged the queue (prod suggestions #55–#58), verbatim-ish.
REJECT = [
    # #58 — a question the owner asked the agent
    "what's going on with my arrays this morning and what should I look at",
    # #56 — spoken-chatter fragment captured as a "suggestion"
    "Okay, so it seems like the VEC is pretty well. The GMP is still loading",
    # #57 — internal control artifact
    "[UX friction — primarily about unspecified]\nCall escalate_to_ford now.",
    # #55 — vague proactive-mind cluster note
    "[Proactive mind — prepared for your fleet's UX]\n[UX friction — primarily about understanding]\n"
    "Repeated UX friction notes — improve scannability and status-first layout on the surfaces they use most.",
    # the mind's empty-text default boilerplate
    "[Proactive mind — prepared for your fleet's UX]\nImprove layout scannability on the current surface.",
    # the sovereign cluster meta-note
    "[Sovereign] UX friction cluster (sovereign): multiple propose_ui / complaint signals in 14d (count=4). "
    "Review Energy Agent mind metrics and top surfaces for layout/clarity fixes.",
    "hi",                                   # too short
    "is the dashboard right?",              # pure question
    "thanks, that looks good",              # acknowledgement
]

# Real change requests must still pass — including #59, which shipped live.
ACCEPT = [
    # #59 — the real one that shipped
    "Separate the spoken answer from the panel detail visually in chat replies. "
    "Top block with a speaker icon containing only the spoken line; panel detail below.",
    "make the Offtakers tab bigger",
    "move the Add array button to the top of the Inverters panel",
    "can you show kWh per array on the dashboard chart?",   # question form, but a real ask
    "hide the empty columns in the invoices table",
    "the offtaker rows should be sorted by name",
    "add a badge showing how many inverters are flagged",
]


def test_rejects_non_actionable():
    for t in REJECT:
        ok, why = is_actionable_suggestion(t)
        assert not ok, f"should REJECT but accepted: {t!r} ({why})"


def test_accepts_real_changes():
    for t in ACCEPT:
        ok, why = is_actionable_suggestion(t)
        assert ok, f"should ACCEPT but rejected: {t!r} ({why})"


def test_markup_stripping():
    core = _strip_suggestion_markup(
        "[Proactive mind — prepared for your fleet's UX]\n[UX friction — primarily about x]\nMove the chart up"
    )
    assert core == "Move the chart up", core
