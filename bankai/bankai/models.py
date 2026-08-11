"""SQLAlchemy models — the ongoing household finance model."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("acct"))
    source: Mapped[str] = mapped_column(String(20), default="csv")  # simplefin | csv
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    institution: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(20), default="checking")  # checking|savings|credit|investment|other
    owner: Mapped[str] = mapped_column(String(40), default="joint")  # ford | spouse | joint
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("txn"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    posted: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)  # negative = outflow
    description: Mapped[str] = mapped_column(Text, default="")
    normalized_desc: Mapped[str] = mapped_column(String(200), default="", index=True)
    category: Mapped[str] = mapped_column(String(60), default="uncategorized", index=True)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped[Account] = relationship(back_populates="transactions")


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (Index("ix_snapshot_account_date", "account_id", "date", unique=True),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("snap"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    date: Mapped[date] = mapped_column(Date)
    balance: Mapped[float] = mapped_column(Float)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("rule"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(30))  # reminder|balance_below|large_transaction|bill_reminder|weekly_digest
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    message: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    firings: Mapped[list["RuleFiring"]] = relationship(back_populates="rule")


class RuleFiring(Base):
    __tablename__ = "rule_firings"
    __table_args__ = (Index("ix_firing_rule_key", "rule_id", "dedupe_key", unique=True),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("fire"))
    rule_id: Mapped[str] = mapped_column(ForeignKey("rules.id"))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

    rule: Mapped[Rule] = relationship(back_populates="firings")


class CategoryRule(Base):
    """Household-taught categorization, persisted. The keyword categorizer gets
    merchants roughly right; it cannot know that a 'Mobile Banking payment to
    CRD 1420' is the household paying its own card (a transfer, not spending)
    or that the Chase ACH is the mortgage. When the copilot reclassifies with
    remember=true, the correction lands here and every future sync applies it
    first — the report stops crying wolf for good, not just for last week."""

    __tablename__ = "category_rules"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("catr"))
    pattern: Mapped[str] = mapped_column(String(120), unique=True)  # case-insensitive substring
    category: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Initiative(Base):
    """A standing PROJECT the copilot is carrying for the household — the spine
    of its ability to complete multi-step work across days instead of one turn
    at a time. The reality generator surfaces things worth owning (the debt
    triage desk, the surrogacy finance file, syncing the planning sheet); an
    initiative is how the copilot actually WORKS one to completion: a goal, a
    plan it wrote, an append-only worklog of what it has done, and the single
    next concrete action it will take when it next has a free cycle.

    An initiative sequences and persists WORK; it grants no new power. Every
    action a step takes still goes through that action's own gate — side-
    effectful things propose, cancellations need a spouse, email is household-
    only. Autonomy here means owning the follow-through, not the authority."""

    __tablename__ = "initiatives"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("init"))
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text, default="")  # what "done" looks like
    plan: Mapped[str] = mapped_column(Text, default="")  # the copilot's own steps
    worklog: Mapped[str] = mapped_column(Text, default="")  # append-only progress
    next_action: Mapped[str] = mapped_column(Text, default="")  # the single next step
    #: active | blocked | done | abandoned
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    #: lower = sooner; ties break on updated_at
    priority: Mapped[int] = mapped_column(Integer, default=100)
    #: set when blocked on the household — what is needed, so it can be surfaced
    blocked_on: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CodeProposal(Base):
    """A concrete, reviewable change the copilot proposes to its OWN code.

    This is the self-improvement loop's memory. Unlike a prose suggestion, a
    proposal carries the actual new file contents, so it can be diffed, tested
    in an isolated sandbox, and shipped with one human approval — or rejected —
    without anyone rewriting it by hand.

    The boundary that makes this safe lives OUTSIDE this row: proposed code is
    DATA until a trusted party ships it. It is tested only in a credential-free,
    network-cut sandbox (agent-authored tests are still code), and merged only
    by a human. The copilot can propose anything; it cannot deploy itself."""

    __tablename__ = "code_proposals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("cp"))
    title: Mapped[str] = mapped_column(String(200))
    rationale: Mapped[str] = mapped_column(Text, default="")
    #: JSON {repo_relative_path: full_new_file_contents}
    files_json: Mapped[str] = mapped_column(Text, default="{}")
    #: which tests prove it, e.g. "tests/test_pending.py" (space/comma separated)
    test_paths: Mapped[str] = mapped_column(String(400), default="")
    #: proposed | awaiting_sandbox | passed | failed | shipped | rejected
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    diff: Mapped[str] = mapped_column(Text, default="")
    test_output: Mapped[str] = mapped_column(Text, default="")
    proposed_by: Mapped[str] = mapped_column(String(60), default="copilot")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LifeFact(Base):
    """One inference in the copilot's model of the household's LIFE — the
    reality generator's memory. Transactions are a diary written in merchants
    and amounts; this table is what the copilot has understood from reading it:
    events ("built a home gym in late July"), rhythms ("cash withdrawal ~$200
    around the 24th, monthly"), predictions ("the two Planet Fitness fees are
    now redundant"), opportunities. Facts carry their evidence and an honest
    confidence, are injected into every turn, and are retired when life moves
    on — a model that never forgets is as wrong as one that never learns."""

    __tablename__ = "life_facts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("life"))
    #: event | rhythm | prediction | opportunity
    kind: Mapped[str] = mapped_column(String(20), default="event", index=True)
    statement: Mapped[str] = mapped_column(String(300))
    evidence: Mapped[str] = mapped_column(Text, default="")
    #: low | medium | high
    confidence: Mapped[str] = mapped_column(String(10), default="medium")
    #: active | confirmed | retired | refuted
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    first_noted: Mapped[date] = mapped_column(Date, default=date.today)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PendingExpense(Base):
    """A spend the household MENTIONED that no data source has shown yet.

    The Apple Card is why this exists: it has no feed — its transactions arrive
    only when someone emails a Wallet export, weeks after the spending. Between
    exports, "put the tires on the Apple Card" is real knowledge with no ledger
    row. It lands here instead: counted when the household asks where money is
    going, matched automatically against the statement when it finally imports,
    and surfaced if it never appears at all (a forgotten export — or a charge
    that was never theirs)."""

    __tablename__ = "pending_expenses"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("pend"))
    mentioned_on: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)  # negative = money out, like Transaction
    description: Mapped[str] = mapped_column(String(240))
    account_hint: Mapped[str] = mapped_column(String(120), default="")  # e.g. "Apple Card"
    speaker: Mapped[str] = mapped_column(String(60), default="")
    #: itemized (a specific spend) | estimate (a rough envelope that shrinks as
    #: itemized detail is attributed to it, and is finalized by the real import)
    kind: Mapped[str] = mapped_column(String(20), default="itemized", index=True)
    #: open | matched | dismissed
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    matched_transaction_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Document(Base):
    """The household document vault: deeds, contracts, policies, estate docs.
    Original file kept on disk; extracted text stored here so the copilot can
    reread and search everything at any time."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("doc"))
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")  # the copilot's own digest
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Property(Base):
    """A tracked real-estate asset. Links the manual property Account (whose
    balance IS the current value) to an address, its specs, and its comps."""

    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("prop"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), unique=True, index=True)
    street: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(20))
    zip_code: Mapped[str] = mapped_column(String(20), default="")
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    beds: Mapped[float | None] = mapped_column(Float, nullable=True)
    baths: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When True, a refresh applies its estimate to the account balance (with a
    # snapshot); when False, estimates are recorded but the value is hand-set.
    auto_update: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped[Account] = relationship()
    comps: Mapped[list["Comp"]] = relationship(back_populates="property_", cascade="all, delete-orphan")
    valuations: Mapped[list["Valuation"]] = relationship(back_populates="property_", cascade="all, delete-orphan")


class Comp(Base):
    """A comparable sale/listing near a tracked property. Sources: rentcast (API),
    manual (dashboard), agent (the copilot heard about a sale)."""

    __tablename__ = "comps"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("comp"))
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # rentcast | manual | agent
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(20), default="sold")  # sold | active | pending
    price: Mapped[float] = mapped_column(Float)
    sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    beds: Mapped[float | None] = mapped_column(Float, nullable=True)
    baths: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    property_: Mapped[Property] = relationship(back_populates="comps")


class Valuation(Base):
    """Every value the tracker computed or was told, with method + evidence.
    applied=True means it became the account balance (and snapshotted)."""

    __tablename__ = "valuations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("val"))
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    value: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(20))  # avm | comps_median | manual | agent
    detail: Mapped[str] = mapped_column(Text, default="")  # evidence: comp count, $/sqft, reasoning
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    property_: Mapped[Property] = relationship(back_populates="valuations")


class AgentAction(Base):
    """Side-effectful actions of the copilot — the audit trail of its reach
    into the world. Default rule: nothing executes without a human clicking
    Approve & run in the portal. ONE standing exception, granted by Ford on
    2026-08-10: subscription cancellations a spouse explicitly instructed
    execute directly (bankai/cancellations.py enforces the boundary) and are
    recorded here as kind='subscription_cancellation', status='executed'."""

    __tablename__ = "agent_actions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("act"))
    kind: Mapped[str] = mapped_column(String(30))  # email_support (more kinds later)
    title: Mapped[str] = mapped_column(String(200))
    rationale: Mapped[str] = mapped_column(Text, default="")  # why the copilot proposes it
    to_email: Mapped[str] = mapped_column(String(200), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    # proposed | executed | declined | failed
    result: Mapped[str] = mapped_column(Text, default="")
    proposed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MemoryNote(Base):
    """Persistent agent memory: small titled notes the copilot writes for itself,
    always injected into its system prompt."""

    __tablename__ = "memory_notes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("mem"))
    title: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ChatMessage(Base):
    """Persistent shared conversation (the SMS group thread lives here)."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("msg"))
    channel: Mapped[str] = mapped_column(String(10), default="sms", index=True)  # sms | web
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    speaker: Mapped[str] = mapped_column(String(60), default="")  # household member name | copilot
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("sync"))
    source: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | error
    detail: Mapped[str] = mapped_column(Text, default="")
