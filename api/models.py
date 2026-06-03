"""
Solar Operator — database models.

Single SQLite file for now (data/solar.db). Schema is Postgres-compatible
so when you outgrow SQLite, swap the URL and you're done. No ORM gymnastics:
SQLAlchemy 2.0 Mapped style, declarative_base, foreign keys, indexes.

Multi-tenant from day one. Every row that belongs to a customer carries
tenant_id and queries are scoped through helpers in db.py.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, Text, UniqueConstraint, Index
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.utcnow()


class Tenant(Base):
    """A paying customer (a solar operator like Bruce)."""
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ten_abc123
    name: Mapped[str] = mapped_column(String(200))
    contact_email: Mapped[str] = mapped_column(String(200), index=True)
    tenant_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sol_live_...
    plan: Mapped[str] = mapped_column(String(32), default="standard")  # standard | comped | legacy_*
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Stripe linkage (added June 2026 for lifecycle + billing portal)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # active, past_due, canceled, comped, trialing

    # Customer prefs (controlled via /account portal)
    report_frequency: Mapped[str] = mapped_column(String(16), default="monthly")
    # weekly | monthly | quarterly
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When True, the operator gets a "[copy]" of every client report email that
    # goes out (records / QA). Wired in delivery.deliver_for_client.
    cc_on_reports: Mapped[bool] = mapped_column(Boolean, default=False)

    # Onboarding wizard state (added June 2026 for the 5-screen signup flow).
    # onboarding_token is a 32-char random string handed to the SPA + passed as
    # Stripe metadata so the post-payment return path can find the pending tenant.
    onboarding_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    onboarding_stage: Mapped[str] = mapped_column(String(20), default="pending_payment")
    # pending_payment | extension | clients | done

    arrays: Mapped[list["Array"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    clients: Mapped[list["Client"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    accounts: Mapped[list["UtilityAccount"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    sessions: Mapped[list["UtilitySession"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class Client(Base):
    """A SUB-CLIENT of a tenant (NEPOOL agent → many clients → each w/ arrays).

    For a small operator like Bruce, his tenant has exactly one Client
    ("Green Mountain Community Solar") that owns all his arrays. For a
    NEPOOL-agent tenant, each of their reporting customers is a separate
    Client — and gets its own per-client workbook delivered to its own email.

    Reports are generated PER CLIENT, not per tenant. The Client decides
    cadence + recipients; the Tenant only sees the consolidated billing.
    """
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Primary contact + extra recipients (comma-separated; cleaner than JSON for SMTP fanout)
    contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cc_emails: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cadence override; falls back to tenant.report_frequency when null
    report_frequency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # weekly | monthly | quarterly | null (inherit)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    # GMP auto-populate (added June 2026 for onboarding wizard Screen 4).
    # When gmp_autopopulate is on, the /v1/sync handler matches an incoming
    # GMP capture by gmp_email OR gmp_username and appends Arrays +
    # UtilityAccounts for this client. GMP lets users log in with either an
    # email address or a username, so we store whichever the operator gave us.
    gmp_email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    gmp_username: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    gmp_autopopulate: Mapped[bool] = mapped_column(Boolean, default=False)
    gmp_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="clients")
    arrays: Mapped[list["Array"]] = relationship(back_populates="client")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_client_per_tenant"),
        Index("ix_clients_tenant_gmp_email", "tenant_id", "gmp_email"),
    )


class Array(Base):
    """A solar array. A logical unit that maps to one OR MORE utility accounts
    (e.g. Bruce's 'Starlake' = 3 GMP accounts summed)."""
    __tablename__ = "arrays"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    client_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    # Which sub-client owns this array. Backfilled to a per-tenant "default
    # Self client" by the migration so existing tenants keep working.
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(40), nullable=True)  # north/south/central
    nepool_gis_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # NEPOOL-GIS asset ID (e.g. "53984" → shown as "Chester (53984)" in reports)
    first_connect_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    solar_adder_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    bill_offset_months: Mapped[int] = mapped_column(Integer, default=1)
    # 1 = bill represents prior month (default for GMP)
    # 0 = bill represents same month (Bruce's Starlake rule)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    tenant: Mapped[Tenant] = relationship(back_populates="arrays")
    client: Mapped["Client | None"] = relationship(back_populates="arrays")
    accounts: Mapped[list["UtilityAccount"]] = relationship(back_populates="array")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_array_per_tenant"),)


class UtilityAccount(Base):
    """A specific account number at a utility provider."""
    __tablename__ = "utility_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    array_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("arrays.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40))  # 'gmp', 'national_grid', ...
    account_number: Mapped[str] = mapped_column(String(40))
    customer_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(200), nullable=True)
    service_address: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # provider-specific raw blob
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    tenant: Mapped[Tenant] = relationship(back_populates="accounts")
    array: Mapped[Array | None] = relationship(back_populates="accounts")
    bills: Mapped[list["Bill"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "account_number", name="uq_account_per_tenant"),
        Index("ix_account_provider_acct", "provider", "account_number"),
    )


class UtilitySession(Base):
    """A captured auth session for a (tenant, provider). Latest row wins.
    Stores the JWT (or whatever the provider uses) for downstream API calls."""
    __tablename__ = "utility_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    api_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="sessions")


class Bill(Base):
    """One pulled bill PDF + extracted metrics."""
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("utility_accounts.id"), index=True)
    bill_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    billing_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kwh_generated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kwh_consumed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    pulled_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    parse_status: Mapped[str] = mapped_column(String(20), default="parsed")  # parsed, failed, partial

    account: Mapped[UtilityAccount] = relationship(back_populates="bills")

    __table_args__ = (
        UniqueConstraint("account_id", "document_number", name="uq_bill_doc"),
        Index("ix_bill_account_date", "account_id", "bill_date"),
    )


class Job(Base):
    """Background jobs. Pull-bills, generate-report, refresh-session, etc."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # pull_bills, generate_report
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # queued, running, succeeded, failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class LoginToken(Base):
    """Magic-link auth: short-lived single-use tokens emailed to customers
    so they can hit /account without a password."""
    __tablename__ = "login_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(200))  # the address it was sent to
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class StripeEvent(Base):
    """Webhook idempotency. Stripe retries failed deliveries; we de-dupe
    on event.id so we never double-process a single event."""
    __tablename__ = "stripe_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="received")
    # received | processed | ignored | error
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
