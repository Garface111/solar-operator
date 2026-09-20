"""The reality generator: the copilot's living model of the household's life.

All of this data IS the household — every transaction a sentence in a diary
written in merchants and amounts. This module is the memory that reading it
produces: LifeFact rows recorded and revised by the copilot through its tools,
rendered into the system prompt of EVERY turn so what the data has taught it is
never rediscovered from scratch, and reviewed on a schedule so the model keeps
up with the life it describes.

Division of labor: the copilot supplies the judgment (what a $463.04 Amazon
pair in a $3,700 late-July cluster probably WAS); this module supplies the
discipline — evidence attached, confidence stated, retired when life moves on.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LifeFact

KINDS = ("event", "rhythm", "prediction", "opportunity")
CONFIDENCES = ("low", "medium", "high")
STATUSES = ("active", "confirmed", "retired", "refuted")

#: What the system prompt can spend on the life model, in characters. Facts are
#: injected newest-first until the budget runs out; the copilot always has
#: list_life_facts for the rest.
RENDER_BUDGET_CHARS = 2600


def record(
    session: Session,
    *,
    statement: str,
    kind: str = "event",
    evidence: str = "",
    confidence: str = "medium",
) -> LifeFact:
    row = LifeFact(
        statement=statement.strip()[:300],
        kind=kind if kind in KINDS else "event",
        evidence=evidence.strip(),
        confidence=confidence if confidence in CONFIDENCES else "medium",
    )
    session.add(row)
    session.flush()
    return row


def visible(session: Session) -> list[LifeFact]:
    """Facts worth carrying into a turn: active and confirmed, newest first."""
    return list(session.execute(
        select(LifeFact)
        .where(LifeFact.status.in_(("active", "confirmed")))
        .order_by(LifeFact.first_noted.desc(), LifeFact.id.desc())
    ).scalars())


def render_for_system(session: Session) -> str:
    """The life model as the system prompt carries it — compact, budgeted."""
    rows = visible(session)
    if not rows:
        return ""
    lines: list[str] = []
    spent = 0
    omitted = 0
    for row in rows:
        line = f"- ({row.kind}, {row.confidence}) {row.statement}"
        if row.evidence:
            line += f" [evidence: {row.evidence[:140]}]"
        if spent + len(line) > RENDER_BUDGET_CHARS:
            omitted += 1
            continue
        spent += len(line)
        lines.append(line)
    if omitted:
        lines.append(f"(+{omitted} more — list_life_facts for the full model)")
    return "\n".join(lines)


def as_dicts(session: Session, include_closed: bool = False) -> list[dict]:
    query = select(LifeFact).order_by(LifeFact.first_noted.desc(), LifeFact.id.desc())
    if not include_closed:
        query = query.where(LifeFact.status.in_(("active", "confirmed")))
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "statement": r.statement,
            "evidence": r.evidence,
            "confidence": r.confidence,
            "status": r.status,
            "first_noted": r.first_noted.isoformat() if r.first_noted else None,
        }
        for r in session.execute(query).scalars()
    ]


def update(
    session: Session,
    fact_id: str,
    *,
    statement: str | None = None,
    evidence: str | None = None,
    confidence: str | None = None,
    status: str | None = None,
) -> LifeFact | None:
    row = session.get(LifeFact, fact_id)
    if row is None:
        return None
    if statement is not None and statement.strip():
        row.statement = statement.strip()[:300]
    if evidence is not None:
        row.evidence = evidence.strip()
    if confidence in CONFIDENCES:
        row.confidence = confidence
    if status in STATUSES:
        row.status = status
    session.flush()
    return row
