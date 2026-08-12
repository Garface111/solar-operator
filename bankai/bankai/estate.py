"""Estate & guardianship completeness — the vault checklist the house counsel keeps.

A household like this one — married, a home, ~$1.09M in trust, and a child
arriving through surrogacy — needs a specific set of legal and protective
documents, and the copilot has flagged three of them (a will, a guardianship
designation, the trust agreement) as missing more than once. This turns that
from a scattered nag into a tracked checklist: what a household in their
situation should have, what the vault actually holds, and the single most
urgent gap to close next.

Matching is deliberately conservative. A checklist item counts as present only
when a real keyword appears in a document's title, the copilot's own summary, or
its extracted text — a stack of documents in the 'estate' category does not by
itself prove a guardianship designation exists. Better to flag a gap the
household can dismiss than to mark a life-or-death document present because a
folder looked full.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document

#: Ordered by urgency for THIS household. "why" is written to be said out loud.
CHECKLIST = [
    {"key": "guardianship", "priority": "urgent",
     "label": "Guardianship designation for your child",
     "keywords": ["guardian", "guardianship", "nomination of guardian"],
     "why": "With a child arriving, this names who would raise them if something "
            "happened to you both — the single most important document a new parent "
            "tends to lack, and it should be in place before the birth."},
    {"key": "will", "priority": "urgent",
     "label": "Will / last will & testament (each spouse)",
     "keywords": ["last will", "testament", "will and testament", "pour-over will"],
     "why": "Directs your assets and is often where guardianship is legally named. "
            "Without one, the state decides both."},
    {"key": "life_insurance", "priority": "high",
     "label": "Life insurance policy (each spouse)",
     "keywords": ["life insurance", "term life", "whole life", "term policy"],
     "why": "Income protection for a household running on one paycheck plus trust "
            "draws, with a child on the way."},
    {"key": "trust_agreement", "priority": "high",
     "label": "Trust agreement — the governing document",
     "keywords": ["trust agreement", "declaration of trust", "trust under agreement",
                  "revocable trust", "irrevocable trust", "grantor trust"],
     "why": "It governs your ~$1.09M in the two trust accounts. You hold the "
            "accounts; I've never seen the document that controls them."},
    {"key": "healthcare_directive", "priority": "high",
     "label": "Healthcare directive / medical POA (each spouse)",
     "keywords": ["healthcare directive", "advance directive", "medical power of attorney",
                  "healthcare proxy", "living will", "advance health"],
     "why": "Names who makes medical decisions if you cannot — one for each of you."},
    {"key": "financial_poa", "priority": "high",
     "label": "Durable financial power of attorney (each spouse)",
     "keywords": ["power of attorney", "durable poa", "financial power of attorney"],
     "why": "Lets your spouse manage money and sign for you if you're incapacitated."},
    {"key": "beneficiaries", "priority": "high",
     "label": "Beneficiary designations (retirement + life)",
     "keywords": ["beneficiary", "beneficiary designation", "designated beneficiary"],
     "why": "These override your will — with a child coming, they need a review."},
    {"key": "surrogacy_agreement", "priority": "high",
     "label": "Surrogacy agency contract & payment schedule",
     "keywords": ["surrogacy", "gestational carrier", "miracle surrogacy", "surrogate"],
     "why": "Your biggest financial project, and I track its payments blind — the "
            "contract would let me build a real paid-vs-remaining ledger."},
    {"key": "deed", "priority": "normal",
     "label": "Recorded grant deed for the home",
     "keywords": ["grant deed", "warranty deed", "recorded deed", "quitclaim"],
     "why": "Proof of ownership and how title is held, which shapes how it transfers."},
    {"key": "title_insurance", "priority": "normal",
     "label": "Title insurance policy",
     "keywords": ["title insurance", "owner's title policy", "owners title"],
     "why": "Protects your ownership against defects in the title."},
    {"key": "homeowners", "priority": "normal",
     "label": "Homeowners insurance policy (coverage terms)",
     "keywords": ["homeowners", "dwelling policy", "ho-3", "home insurance", "hazard insurance"],
     "why": "The actual coverage terms, not just that Chase escrows the premium."},
    {"key": "umbrella", "priority": "normal",
     "label": "Umbrella liability policy",
     "keywords": ["umbrella policy", "personal liability umbrella", "excess liability"],
     "why": "Cheap liability protection that matters more once you have real assets "
            "and a child."},
    {"key": "marriage_cert", "priority": "normal",
     "label": "Marriage certificate",
     "keywords": ["marriage certificate", "certificate of marriage"],
     "why": "Needed for spousal legal, benefit, and estate matters."},
]

_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2}


def _haystack(doc: Document) -> str:
    return " ".join([
        doc.title or "", doc.summary or "", (doc.content_text or "")[:4000],
    ]).lower()


def _find(item: dict, docs: list[Document]) -> Document | None:
    kws = [k.lower() for k in item["keywords"]]
    for doc in docs:
        hay = _haystack(doc)
        if any(k in hay for k in kws):
            return doc
    return None


def checklist_status(session: Session) -> dict:
    """What the household should have vs. what the vault holds."""
    docs = list(session.execute(select(Document)).scalars())
    items = []
    for item in CHECKLIST:
        match = _find(item, docs)
        items.append({
            "key": item["key"],
            "label": item["label"],
            "priority": item["priority"],
            "why": item["why"],
            "present": match is not None,
            "document": {"id": match.id, "title": match.title} if match else None,
        })
    present = [i for i in items if i["present"]]
    missing = [i for i in items if not i["present"]]
    missing.sort(key=lambda i: _PRIORITY_ORDER.get(i["priority"], 3))
    return {
        "items": items,
        "present_count": len(present),
        "total": len(items),
        "completeness_pct": round(100 * len(present) / len(items)) if items else 0,
        "missing": missing,
        "top_missing": missing[:3],
        "note": (
            "Present means a document matched by keyword — verify a match is really "
            "the right document (titles can be cryptic). Raise the top missing item "
            "gently, one at a time; guardianship and a will are the urgent pair now "
            "that a child is coming, and anything legal should be reviewed by a "
            "licensed attorney."
        ),
    }
