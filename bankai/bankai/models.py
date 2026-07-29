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
    """Side-effectful actions the copilot proposes (cancel a subscription by
    emailing support, etc.). NOTHING here executes without a human clicking
    Approve & run in the portal; every outcome is recorded, so this table is the
    audit trail of the copilot's reach into the world."""

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
