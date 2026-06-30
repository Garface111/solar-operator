"""
NEPOOL Operator — database models.

Single SQLite file for now (data/solar.db). Schema is Postgres-compatible
so when you outgrow SQLite, swap the URL and you're done. No ORM gymnastics:
SQLAlchemy 2.0 Mapped style, declarative_base, foreign keys, indexes.

Multi-tenant from day one. Every row that belongs to a customer carries
tenant_id and queries are scoped through helpers in db.py.
"""
from __future__ import annotations
from datetime import datetime, date
from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, Date, ForeignKey, JSON, Text,
    LargeBinary, UniqueConstraint, Index
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Encryption-at-rest for vendor credentials. EncryptedJSON/EncryptedStr are
# transparent TypeDecorators: pure pass-through when SO_CONFIG_KEY is unset
# (storage byte-identical to the old plaintext columns), Fernet-encrypted when
# it is set. See api/crypto.py for the full design + rollout runbook.
from .crypto import EncryptedJSON, EncryptedStr


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.utcnow()


class Tenant(Base):
    """A paying customer (a solar operator like Bruce)."""
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ten_abc123
    # Legacy single-name field. Pre-Jun-2026 this was overloaded as BOTH the
    # operator's personal name AND their company name. Still populated for
    # backward compat with code paths we haven't migrated yet — it now mirrors
    # company_name on write. Will be removed in a future cleanup once nothing
    # reads it. New code MUST use operator_name / company_name explicitly.
    name: Mapped[str] = mapped_column(String(200))
    # ── Split-name fields (Jun 2026) ─────────────────────────────────────
    # operator_name = the human's personal name ("Ford Genereaux") — used in
    # report signoffs, magic-link greetings, internal alerts, welcome emails.
    # company_name  = the business name ("Genereaux Solar Co.") — used in the
    # email From: display, report titles, Stripe customer record.
    # Both nullable: existing tenants are backfilled with company_name=name,
    # operator_name=NULL (operator fills it on the Settings card).
    operator_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_email: Mapped[str] = mapped_column(String(200), index=True)
    tenant_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sol_live_...
    plan: Mapped[str] = mapped_column(String(32), default="standard")  # standard | comped | legacy_* | demo
    # Which EnergyAgent product this tenant pays for — drives which Stripe price
    # their subscription uses (see stripe_helpers.array_price_id_for_product).
    #   "nepool" → NEPOOL Operator verifier ($15/array graduated + $250 setup)
    #   "array_operator" → Array Operator owner app (1st array free, $9/$8/$6.50)
    # Default "nepool" so every existing tenant is unchanged.
    product: Mapped[str] = mapped_column(
        String(32), default="nepool", server_default="nepool", nullable=False
    )
    # Optional billing-plan override WITHIN a product. For Array Operator, selects
    # which meter the tenant pays on:
    #   null / "" / "kwh" → per-kWh monitoring meter (the AO default)
    #   "invoicing"        → per-OFFTAKER licensed plan (auto-invoicing only; $100
    #                        base incl 4 offtakers + $25/offtaker, $250 setup —
    #                        api/pricing_ao_invoicing.py)
    # Ignored for NEPOOL (always per-array). Null for every existing tenant so
    # nothing changes until a tenant is explicitly put on the invoicing plan.
    billing_plan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Shared read-only demo tenant (June 2026). When True, every visitor who
    # clicks the homepage "Try it" magic link signs in as THIS tenant, and all
    # mutating endpoints refuse with a friendly 403 (see account.require_not_demo).
    # Default False for every real tenant — only the seeded demo tenant gets True.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    # Stripe linkage (added June 2026 for lifecycle + billing portal)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # active, past_due, canceled, comped, trialing

    # Customer prefs (controlled via /account portal)
    report_frequency: Mapped[str] = mapped_column(String(16), default="quarterly")
    # weekly | monthly | quarterly — quarterly is the operator default (NEPOOL
    # reports quarterly; monthly was an engineer-default, corrected V1 Jun 2026)
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When True, the operator gets a "[copy]" of every client report email that
    # goes out (records / QA). Wired in delivery.deliver_for_client.
    cc_on_reports: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Automatic warranty claims policy (Array Operator, June 2026) ──────
    # The owner's send policy for auto-drafted warranty claims. Full control:
    #   "manual" → agent drafts, owner approves each send (default — safe)
    #   "auto"   → agent files the instant a failure is confirmed
    #   "delay"  → agent queues and files after `claim_grace_hours` unless cancelled
    # Overridable per-claim via WarrantyClaim.send_mode. See api/warranty_claims.py.
    claim_send_mode: Mapped[str] = mapped_column(
        String(16), default="manual", server_default="manual", nullable=False
    )
    claim_grace_hours: Mapped[int] = mapped_column(
        Integer, default=24, server_default="24", nullable=False
    )

    # ── Billing rate (Array Operator, Jun 2026) ──────────────────────────
    # The operator's GLOBAL default $/kWh used to price a customer's produced
    # generation when that customer has no per-customer override
    # (BillingReportSubscription.rate_per_kwh). Nullable → when null, pricing
    # falls back to the legacy VT default (delivery.MANUAL_TARIFF ×
    # MANUAL_BILLING_RATE). Set/read via the Reports tab global-rate control.
    default_billing_rate_per_kwh: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    # ── Discount billing model (Jun 2026) ────────────────────────────────
    # The customer pays the NET RATE minus a DISCOUNT (the operator's savings
    # offer): invoice = produced kWh × net_rate × (1 − discount). Both are the
    # operator's GLOBAL defaults, overridable per customer on the subscription.
    #   default_discount_pct      — fraction in [0,1); null → DEFAULT_DISCOUNT (0.10 = 10% off)
    #   default_net_rate_per_kwh  — $/kWh the discount applies to; null → VT default tariff
    # (Supersedes the flat default_billing_rate_per_kwh above, kept only for
    # backward-compat; new pricing reads the discount model.)
    default_discount_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    default_net_rate_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Email customization (V2, June 2026) ──────────────────────────────
    # Let the tenant (a NEPOOL stamping agent) control how reports go out
    # under their professional name. All nullable → null means "use the
    # built-in default". See api/email_templates.py for rendering + defaults.
    send_from_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # From address for client report emails. null → platform default
    # (admin@solaroperator.org). Custom addresses fall back to the platform
    # default at send time if Resend rejects an unverified domain.
    send_from_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Friendly From display name. null → tenant.name.
    email_subject_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_body_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_signoff: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Merge-tag templates ({{client_name}}, {{tenant_name}}, {{quarter}}, …).
    # null → built-in default template.
    send_mode: Mapped[str] = mapped_column(String(20), default="to_client")
    # to_client = primary recipient is the client (+ cc tenant if cc_on_reports)
    # to_me     = primary recipient is tenant.contact_email (tenant forwards);
    #             subject/body still rendered with client_name.

    # Extension heartbeat (W3-19, June 2026). Updated by the background.js
    # periodic ping so the onboarding screen can distinguish "extension active"
    # from "extension installed but not seen recently."
    extension_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Deferred billing (June 2026). trial_ends_at is set when the operator
    # completes setup-mode checkout; the cron job creates the real subscription
    # at trial end. trial_extended tracks whether we've already done the 3-day
    # zero-array grace extension so we only do it once.
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stripe_payment_method_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trial_extended: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Inverter down/underperformance email alerts (Array Operator, Jun 2026) ──
    # Owner-controlled: when ON, the alert sweep emails inverter_alert_email (or
    # contact_email) whenever an inverter goes dark OR drops below
    # inverter_alert_threshold_pct of its array peers for at least the grace window.
    # threshold = sensitivity slider (e.g. 50 → alert under 50% of peers); a fully
    # dark inverter always trips regardless. De-dup so we email once per incident.
    # ON BY DEFAULT (2026-06-24): the fleet watch should page an operator the
    # moment an inverter goes down without them first hunting for a toggle.
    # migrate.py one-time-flips existing tenants false→true. comm_gap false
    # positives from the extension capture cadence are suppressed in
    # inverter_alert_sweep, so "on" never means false spam.
    inverter_alerts_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False)
    inverter_alert_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    inverter_alert_threshold_pct: Mapped[int] = mapped_column(
        Integer, default=50, server_default="50", nullable=False)
    inverter_alert_grace_hours: Mapped[int] = mapped_column(
        Integer, default=12, server_default="12", nullable=False)
    # No-upfront-payment: set the moment the ~3-day "trial ending, no card on
    # file" reminder is sent, so the scheduler fires it exactly once regardless
    # of tick cadence (replaces the fragile 1-day rolling window). NULL = not
    # yet reminded.
    trial_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Second, urgent "last chance" reminder ~2 days before trial end, sent
    # only after the early heads-up. Separate field so each touch is exactly-once.
    trial_final_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # GMP reauth-alert cooldown: when we last emailed this operator to re-login.
    # Suppresses repeat reauth/internal alerts within 7d even if refresh re-crosses
    # the failure threshold (flapping / duplicate sessions) — bounds alert spam.
    gmp_reauth_alert_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Password-based login (June 2026). Nullable — null means magic-link only.
    # bcrypt hash (passlib, cost 12). Never expose this field in API responses.
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Onboarding wizard state (added June 2026 for the 5-screen signup flow).
    # onboarding_token is a 32-char random string handed to the SPA + passed as
    # Stripe metadata so the post-payment return path can find the pending tenant.
    onboarding_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    onboarding_stage: Mapped[str] = mapped_column(String(20), default="extension")
    # extension | clients | done
    # (Was 'pending_payment' pre no-upfront-payment — operators are now in a live
    # trial the moment they finish signup, so there is no pre-payment stage.)
    onboarding_array_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Array count entered at signup (Path A: sum of array entries; Path B:
    # operator's numeric estimate). NULL for tenants who pre-date this field.
    # Used only for the "You're all set!" dashboard milestone check.

    # ── Consent / authorization-to-access record (Jun 2026) ──────────────
    # The Terms/Privacy + account-access-authorization version the owner accepted
    # at signup, the moment they accepted, and the source IP — durable proof of
    # consent for the access the extension performs on their behalf. NULL for
    # tenants who pre-date the consent gate.
    consent_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consent_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Cross-product sibling link (Jun 2026) ────────────────────────────
    # A person can own a NEPOOL tenant AND an Array Operator tenant on the same
    # email — two SEPARATE rows with different `product`. When their two tenants
    # are linked (opt-in, verified-email-scoped, set bidirectionally by
    # api.tenant_link), ONE extension install's captures fan out into BOTH: the
    # GMP/utility + inverter telemetry the extension reports for one product's
    # tenant is ALSO replayed into the linked sibling, so a single install feeds
    # the NEPOOL reports side and the AO monitoring side at once.
    #
    # Self-referential, nullable, NOT a hard FK constraint (kept loose so nulling
    # one side never trips referential integrity and an orphaned id is harmless —
    # the fan-out path re-validates the sibling exists + is the other product
    # before writing). Null for every existing tenant → nothing fans out until a
    # link is deliberately established. Reversible by nulling both sides.
    linked_tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # ── Operator-level BYO generation spreadsheet (Jun 2026) ─────────────
    # Mirrors the per-subscription tracker (BillingReportSubscription.tracker_*)
    # but keyed to the TENANT instead of one offtaker: the operator uploads ONE
    # running generation sheet for their whole operation; we detect its columns
    # (api/billing/sheet_tracker.detect_structure) and keep it on file so they
    # can download it. ADDITIVE + nullable — NULL for every existing tenant, no
    # auto-append in v1 (deferred), and it never touches the live invoice/billing
    # path. Same storage shape as the per-sub tracker so the frontend renderer is
    # reusable.
    #   tracker_workbook — the stored xlsx bytes (CSV uploads normalized to xlsx)
    #   tracker_filename — original upload name (for the download filename)
    #   tracker_map      — the detected structure (sheet/header_row/columns/…)
    #   tracker_updated_at — last upload time
    tracker_workbook: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    tracker_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    tracker_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tracker_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    arrays: Mapped[list["Array"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    clients: Mapped[list["Client"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    accounts: Mapped[list["UtilityAccount"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    sessions: Mapped[list["UtilitySession"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class InverterAlertState(Base):
    """Tracks open inverter-down incidents so the email sweep alerts ONCE per
    incident (not every tick) and honors the per-tenant grace window. A row exists
    while an inverter is flagged; it's deleted when the inverter recovers, so the
    next failure is a fresh incident. See api/inverter_alert_sweep.py."""
    __tablename__ = "inverter_alert_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    # "<array_id>|<inverter_id-or-name>" — stable per inverter within an array
    incident_key: Mapped[str] = mapped_column(String(200), index=True)
    first_flagged_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # GMP auto-populate (added June 2026 for onboarding wizard Screen 4).
    # When gmp_autopopulate is on, the /v1/sync handler matches an incoming
    # GMP capture by gmp_email OR gmp_username and appends Arrays +
    # UtilityAccounts for this client. GMP lets users log in with either an
    # email address or a username, so we store whichever the operator gave us.
    gmp_email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    gmp_username: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    gmp_autopopulate: Mapped[bool] = mapped_column(Boolean, default=True)
    gmp_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # VEC auto-populate (added June 2026 — mirrors GMP triple for the VEC provider).
    vec_email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    vec_username: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    vec_autopopulate: Mapped[bool] = mapped_column(Boolean, default=True)
    vec_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # V2 (feat/v2-rec-fuels): the client's default generation fuel, captured in
    # the onboarding wizard. Manually-entered arrays without their own fuel
    # inherit it, and arrays auto-populated later by /v1/sync are tagged with it
    # — so a wind/hydro/digester/storage operator who uses autopop (and thus
    # enters no arrays at onboarding) still gets fuel-correct arrays + reports.
    # Defaults to 'solar' so every existing client is byte-identical.
    default_fuel_type: Mapped[str] = mapped_column(
        String(20), default="solar", server_default="solar"
    )

    # Per-client email delivery health (W2-6, June 2026). Populated by the Resend
    # webhook (api/resend_webhook.py) on bounced/delivered/complained events.
    # NOTE distinct from last_delivery_at (when WE sent): these reflect what
    # Resend reports actually happened to the recipient's inbox.
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_bounced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_bounce_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Placeholder flag (June 2026): set when the onboarding flow seeded this
    # row as "Your first client" because the operator didn't pre-enter any
    # clients (Path B / array-count-only path). The dashboard treats these
    # specially — surfaces a 'Rename this to your real client name' prompt
    # and uses it as the anchor for the first-visit walkthrough. As soon
    # as the operator renames it OR adds arrays through the extension,
    # is_placeholder gets cleared.
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)

    # Stamped when the operator explicitly edits client.name via the dashboard
    # PATCH endpoint. Re-captures check this: if non-null, the operator curated
    # the name — skip the autopop name override entirely.
    name_edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Canvas position (sandbox canvas v1, June 2026). null = not yet placed,
    # auto-arranged on first visit. canvas_pinned reserves a future "lock
    # position" toggle so the operator can anchor frequently-used clients.
    canvas_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    canvas_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    canvas_pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    tenant: Mapped[Tenant] = relationship(back_populates="clients")
    arrays: Mapped[list["Array"]] = relationship(back_populates="client")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_client_per_tenant"),
        Index("ix_clients_tenant_gmp_email", "tenant_id", "gmp_email"),
        Index("ix_clients_tenant_vec_email", "tenant_id", "vec_email"),
    )


class ReportDelivery(Base):
    """Durable per-client record of one SCHEDULED report batch (June 2026).

    Closes the "did it actually go out, and did it land?" gap the Resend webhook
    docstring calls out. scheduler._deliver_clients_with_frequency writes one row
    per client it processes — sent, skipped (no data), or failed. ~2h later
    jobs/report_digests.run_delivery_receipts() reads these rows, cross-references
    per-client Resend delivery health (Client.last_delivered_at / last_bounced_at
    vs sent_at), emails the operator a "what went out to whom + confirmed
    delivered" receipt, and stamps receipt_sent_at so each batch is reported
    exactly once. Denormalized (client_name/recipient copied in, no FKs) so it
    survives client/tenant cleanup as a historical record.
    """
    __tablename__ = "report_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_name: Mapped[str] = mapped_column(String(200))
    recipient: Mapped[str | None] = mapped_column(String(400), nullable=True)
    cadence: Mapped[str] = mapped_column(String(16))  # weekly | monthly | quarterly
    # sent | skipped_empty | no_recipient | send_failed | failed | inactive
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    # Stamped when the operator receipt covering this row has been sent (NULL =
    # not yet receipted). The receipt job filters on this to be exactly-once.
    receipt_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        Index("ix_report_deliveries_tenant_pending", "tenant_id", "receipt_sent_at"),
    )


class Array(Base):
    """A generation array that mints renewable-energy certificates. A logical
    unit that maps to one OR MORE utility accounts (e.g. Bruce's 'Starlake' =
    3 GMP accounts summed).

    Historically solar-only; V2 (feat/v2-rec-fuels) generalizes the same
    capture → array×months MWh → REC=floor(MWh) → attestation pipeline to any
    REC-bearing fuel via `fuel_type`. Solar behavior is unchanged: fuel_type
    defaults to 'solar' and cert_registry NULL means the implicit NEPOOL-GIS
    registry that solar has always used."""
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
    # When True, this array is excluded from reports and billing (e.g. Pittsfield:
    # below the REC-threshold, operator can't sell its RECs). Data still flows.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    reassigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # SolarEdge Monitoring API integration (feat/solaredge-adapter).
    # The api_key is a vendor secret → encrypted at rest via EncryptedStr
    # (Fernet, keyed on SO_CONFIG_KEY; pass-through plaintext when unset, so the
    # column is byte-identical until a key is provisioned). site_id is a
    # non-secret identifier and stays plaintext so existing IS NOT NULL / value
    # reads on it are unaffected. See api/crypto.py.
    solaredge_api_key: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    solaredge_site_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Geolocation + array geometry (feat: predicted-vs-actual production) ──
    # Resolved ONCE (lazily, on first forecast request) by geocoding the array's
    # linked UtilityAccount.service_address, then cached here so we never re-hit a
    # geocoder per-request. NULL = not yet geocoded (or no address to geocode).
    # All optional so every existing array is byte-identical until first forecast.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Provenance of the lat/lng so the UI can be honest about precision:
    # 'census' (rooftop-ish, US street match) | 'nominatim' | 'open-meteo' (city
    # centroid, coarse) | 'manual' (operator-entered). NULL while ungeocoded.
    geocode_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # The address string we actually geocoded (audit trail; lets us re-geocode if
    # the source address later changes).
    geocoded_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    geocoded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Array geometry for the plane-of-array irradiance model. When the operator
    # hasn't told us, we DEFAULT (tilt ≈ latitude, azimuth = 180° true-south) and
    # surface that assumption loudly in the UI. An operator override sets these +
    # flips geometry_source to 'manual'.
    tilt_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    azimuth_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    geometry_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'default' | 'manual'

    # V2 (feat/v2-rec-fuels): the REC-minting fuel this array represents.
    # Allowed values: solar | wind | hydro | digester | storage. Defaults to
    # 'solar' so every existing array and all solar logic is byte-identical.
    fuel_type: Mapped[str] = mapped_column(
        String(20), default="solar", server_default="solar"
    )
    # Which certificate registry the RECs are issued through, e.g.
    # 'NEPOOL-GIS' or 'LIHI' (Low Impact Hydropower Institute). NULL means the
    # implicit NEPOOL-GIS registry that solar has always used.
    cert_registry: Mapped[str | None] = mapped_column(String(40), nullable=True)

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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Canvas position (sandbox canvas v1, June 2026). Used for unclassified
    # accounts floating outside any client node.
    canvas_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    canvas_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    canvas_pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    # Original client this account's login belonged to before being manually
    # moved in the sandbox. NULL while the account is still under its original
    # owner. Used by the sandbox to keep moved logins visually separate from
    # the target client's existing same-utility login (so the operator can
    # tell two GMP logins apart and undo the move easily).
    login_origin_client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=True, index=True,
    )

    # The client name that was auto-populated at first capture for this account.
    # Used to detect whether the operator has since manually edited the client
    # name (if client.name != captured_client_name, respect the edit on re-capture).
    captured_client_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # True when this account is a residential (non-generation) customer with no
    # NEPOOL participation. Set during /v1/sync for GMP accounts that lack
    # solarNetMeter=true and groupNetMetered=true. Residential accounts are
    # persisted but never trigger Client/Array auto-creation.
    is_residential: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="accounts")
    array: Mapped[Array | None] = relationship(back_populates="accounts")
    bills: Mapped[list["Bill"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "account_number", name="uq_account_per_tenant"),
        Index("ix_account_provider_acct", "provider", "account_number"),
    )


class UtilitySession(Base):
    """A captured auth session for one utility LOGIN.

    Stores the JWT (or whatever the provider uses) for downstream API calls.
    Keyed by `(tenant, provider, customer_number)` — the login's identity — so
    an operator who logs into multiple distinct utility customers (e.g. a
    separate GMP login per client) keeps EVERY login independently usable for
    ongoing scraping. Selection is per-account by customer_number (see
    api/sessions.py); a capture re-binds (upserts) its identity's row in place.
    Rows whose customer_number is NULL (legacy captures, or providers that don't
    expose a customer id) fall back to the latest-per-provider behavior."""
    __tablename__ = "utility_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    # The login identity this session belongs to (GMP personId / SmartHub
    # custNbr), shared by all accounts captured under this login. NULL for
    # legacy rows / providers without a customer id → latest-per-provider.
    customer_number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    api_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    refresh_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="sessions")


class Bill(Base):
    """One pulled bill — the full energy record, not just generation.

    DATA SPONGE: the JSON pull keeps EVERYTHING GMP exposes per billing period —
    generation, consumption, sent-to-grid, cost, rate, net-metering credits,
    supplier — plus the entire raw bill in raw_json so we never lose a field we
    haven't modeled yet. This makes the account the owner's system of record for
    their whole energy life (years of data they can't easily get elsewhere), and
    the switching cost compounds with every billing period absorbed.
    """
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
    # ── Full energy-record fields (the sponge) ──────────────────────────────
    kwh_sent_to_grid: Mapped[float | None] = mapped_column(Float, nullable=True)
    kwh_gross_generated: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_net_metered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)        # $ billed this period
    net_credit: Mapped[float | None] = mapped_column(Float, nullable=True)        # $ net-metering credit
    # Gross SOLAR credit (EXCESS + SOLCRED from the page-2 line items) the array
    # earned this period — the offtaker billing basis (excess kWh already lives in
    # kwh_sent_to_grid). NULL means banked/unknown → the offtaker invoice SKIPS
    # rather than over-charging from gross kWh × a flat rate. See
    # rate_schedule.solar_credit_from_bill.
    solar_credit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_rate_cents_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)  # blended ¢/kWh
    supplier: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The ENTIRE raw bill JSON — the true sponge: never lose a field we don't
    # model yet, so a future view can mine it without a re-pull.
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    document_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Durable bill-PDF bytes (Jun 2026). pdf_path points at Railway's EPHEMERAL
    # disk (wiped on redeploy), so for anything that must survive — e.g. the
    # auto-attach-GMP-bill feature reading via api/reports/gmp_bill_pdf_read —
    # the actual PDF bytes are persisted in-row here. Nullable: only populated
    # when the pull captures the PDF (current-bill path); null = not captured.
    pdf_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pdf_content_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    pulled_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    parse_status: Mapped[str] = mapped_column(String(20), default="parsed")  # parsed, failed, partial

    account: Mapped[UtilityAccount] = relationship(back_populates="bills")

    __table_args__ = (
        UniqueConstraint("account_id", "document_number", name="uq_bill_doc"),
        Index("ix_bill_account_date", "account_id", "bill_date"),
    )


class RateSchedule(Base):
    """A pre-propagated blended retail rate cell — the auto-applied net rate for
    billing (Jun 2026). The correct rate depends on FOUR dimensions:
      • utility       (provider code: gmp/vec/wec/stowe/…)
      • location      (location_class: VT region north/central/south, or '*')
      • array age     (age_bucket: 'le11' = ≤11 yrs since first_connect, 'gt11' = older)
      • month/period  (effective_start..effective_end — rates reset ~every 2 yrs)

    Rows are DERIVED FROM CAPTURED BILLS (api/rate_schedule.derive_*), never
    invented: blended_rate_per_kwh is the measured $/kWh from real GMP bill line
    items over the window, with sample_size + source_note for auditability. To
    handle the biennial reset, add a new row with the next effective window; the
    resolver auto-picks it once the billing month rolls into that range.
    """
    __tablename__ = "rate_schedule"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(8), default="VT", server_default="VT", index=True)
    utility: Mapped[str] = mapped_column(String(24), index=True)        # provider code, or '*' = any
    location_class: Mapped[str] = mapped_column(String(24), default="*", server_default="*")
    age_bucket: Mapped[str] = mapped_column(String(8), default="*", server_default="*")  # le11 | gt11 | *
    effective_start: Mapped[date] = mapped_column(Date, index=True)
    effective_end: Mapped[date | None] = mapped_column(Date, nullable=True)  # null = open-ended
    blended_rate_per_kwh: Mapped[float] = mapped_column(Float)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # bills measured
    source_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        Index("ix_rate_lookup", "state", "utility", "location_class", "age_bucket", "effective_start"),
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


class SpongeProgress(Base):
    """Live progress of an onboarding history-absorb ("data sponge") run, so the
    UI can render a real progress bar. One row per (tenant, provider) absorb;
    re-running an absorb resets it. The frontend polls GET /account/sponge.
    """
    __tablename__ = "sponge_progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="gmp")
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|done|error
    accounts_total: Mapped[int] = mapped_column(Integer, default=0)
    accounts_done: Mapped[int] = mapped_column(Integer, default=0)
    bills_absorbed: Mapped[int] = mapped_column(Integer, default=0)
    years_covered: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_sponge_tenant_provider"),
    )


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
    persist_session: Mapped[bool] = mapped_column(Boolean, default=True)


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


class DeleteHistory(Base):
    """Undo history for soft-deleted records. One row per delete operation
    (single or bulk). payload stores the IDs of every row that was set to
    deleted_at, grouped by table, so undo can reverse the full cascade.
    Rows with expires_at in the past can no longer be undone; the daily
    cleanup job hard-deletes the underlying data at 30 days."""
    __tablename__ = "delete_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    undo_token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # {"clients": [...], "arrays": [...], "utility_accounts": [...]}
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClientMergeDismissal(Base):
    """Records the operator's explicit 'keep separate' decision for a
    suggested duplicate pair so we don't nag them again.

    Pair is normalized as (min_id, max_id) so we only need one row per
    pair regardless of which side suggested merging.
    """
    __tablename__ = "client_merge_dismissals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    client_a_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), index=True)
    client_b_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), index=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "client_a_id", "client_b_id",
                         name="uq_merge_dismissal_pair"),
    )


class ArrayMergeDismissal(Base):
    """Like ClientMergeDismissal but for Array pairs. Same shape:
    normalize the pair as (min_id, max_id) so a dismissal in either
    direction is remembered once."""
    __tablename__ = "array_merge_dismissals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    array_a_id: Mapped[int] = mapped_column(Integer, ForeignKey("arrays.id"), index=True)
    array_b_id: Mapped[int] = mapped_column(Integer, ForeignKey("arrays.id"), index=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "array_a_id", "array_b_id",
                         name="uq_array_merge_dismissal_pair"),
    )


class DailyGeneration(Base):
    """One row per (array, calendar day) — authoritative source for monthly
    generation totals when the operator uploads a daily CSV.

    When a month has any DailyGeneration rows for an array, the writer uses
    DailyGeneration EXCLUSIVELY for that month (no Bill data mixed in —
    double-counting would be worse than either alone).
    """
    __tablename__ = "daily_generation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    array_id: Mapped[int] = mapped_column(Integer, ForeignKey("arrays.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kwh: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="csv")
    # csv | manual | gmp_portal_scrape | extension_pull | bill_prorate
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("array_id", "day", name="uq_daily_array_day"),
    )

    array: Mapped["Array"] = relationship()
    tenant: Mapped["Tenant"] = relationship()


class GmpUsageRaw(Base):
    """THE SPONGE for GMP daily-interval usage. One row per (account, fetched
    window) holding the VERBATIM CSV payload GMP returned from
    /api/v2/usage/{acct}/download. This is the authoritative source of record —
    the modeled GmpDailyGeneration rows are a queryable convenience derived from
    these blobs and can always be re-derived in place with NO re-pull (mirrors
    Bill.raw_json + sponge.rederive_from_raw).

    Why per-window (not per-day) raw: GMP serves 15-min intervals in date-range
    windows (≤90 days/request — a 1-year request 503-times-out server-side), so
    the natural raw unit is the window we fetched. Ford attaches the raw GMP
    source to invoices later, so we keep every byte.
    """
    __tablename__ = "gmp_usage_raw"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("utility_accounts.id"), index=True)
    account_number: Mapped[str] = mapped_column(String(40), index=True)
    window_start: Mapped[date] = mapped_column(Date, index=True)   # requested window start
    window_end: Mapped[date] = mapped_column(Date)                 # requested window end
    fmt: Mapped[str] = mapped_column(String(8), default="csv")     # the GMP ?format= value
    http_status: Mapped[int] = mapped_column(Integer, default=200) # 200 ok, 404 below floor, etc.
    row_count: Mapped[int] = mapped_column(Integer, default=0)     # interval rows parsed from this blob
    interval_min: Mapped[date | None] = mapped_column(Date, nullable=True)  # earliest IntervalStart seen
    interval_max: Mapped[date | None] = mapped_column(Date, nullable=True)  # latest IntervalStart seen
    raw_csv: Mapped[str | None] = mapped_column(Text, nullable=True)  # the VERBATIM payload (the sponge)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    __table_args__ = (
        # One authoritative row per (account, exact window). Re-fetching the same
        # window overwrites in place — idempotent, never duplicates the sponge.
        UniqueConstraint("account_id", "window_start", "window_end", name="uq_gmp_raw_window"),
        Index("ix_gmp_raw_acct_window", "account_id", "window_start"),
    )


class GmpDailyGeneration(Base):
    """Modeled, queryable daily generation derived from GmpUsageRaw — one row per
    (utility account, calendar day). Kept SEPARATE from DailyGeneration on
    purpose: a GMP account == one meter/ServiceAgreement, but an Array may sum
    several GMP meters (e.g. Bruce's Starlake = 3 sub-meters). Storing per-ACCOUNT
    here avoids the (array_id, day) collision and keeps the raw→modeled mapping
    1:1 and re-derivable. The READ contract aggregates per-array on read.

    source is always 'gmp_api' (the utility meter's settled generation view) so
    it is never confused with independent inverter production (solaredge/extension)
    by the reconciliation engine.
    """
    __tablename__ = "gmp_daily_generation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("utility_accounts.id"), index=True)
    account_number: Mapped[str] = mapped_column(String(40), index=True)
    # Denormalized array link at derive time, for fast per-array reads. Nullable:
    # a GMP account may not be mapped to an array yet (residential / unassigned).
    array_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("arrays.id"), nullable=True, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kwh: Mapped[float] = mapped_column(Float)              # Σ of that day's 15-min interval Quantity (kWh)
    interval_count: Mapped[int] = mapped_column(Integer, default=0)  # # of intervals summed (96 = full day)
    source: Mapped[str] = mapped_column(String(16), default="gmp_api")
    derived_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("account_id", "day", name="uq_gmp_daily_account_day"),
        Index("ix_gmp_daily_acct_day", "account_id", "day"),
        Index("ix_gmp_daily_array_day", "array_id", "day"),
    )


class InverterConnection(Base):
    """A per-array connection to an inverter vendor's cloud API.

    Generalizes the SolarEdge-only integration: one row per array, `vendor`
    selects the adapter (solaredge | fronius | sma | chint) and `config` holds
    that vendor's credentials/IDs as a JSON blob (shape defined by the vendor
    module's FIELDS).

    Backward compat: arrays connected before this table existed carry their
    SolarEdge creds on Array.solaredge_api_key/solaredge_site_id. Readers treat
    those as a virtual {vendor: "solaredge"} connection when no row exists here.

    ENCRYPTION AT REST: `config` holds the vendor's credentials/tokens, so it is
    an EncryptedJSON column (Fernet, keyed on SO_CONFIG_KEY). When the key is
    unset it is a pure pass-through (plaintext JSON, byte-identical to the old
    JSON column) so rollout is safe + reversible; when set, a DB dump yields
    ciphertext. Encryption is transparent — every reader still does
    `conn.config["api_key"]`. The stored type is TEXT (not native json) so the
    ciphertext envelope fits; api/migrate.py widens the legacy Postgres `json`
    column to TEXT before any key is provisioned. See api/crypto.py.
    """
    __tablename__ = "inverter_connections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    array_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("arrays.id"), unique=True, index=True
    )
    vendor: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(EncryptedJSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="unverified")
    # unverified | ok | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When the one-time DEEP HISTORY backfill last succeeded for this connection.
    # The nightly pull only reaches ~90 days, so a freshly-connected vendor would
    # show just the current year in Trends. The self-healing backfill pulls the
    # vendor's full multi-year daily history into DailyGeneration on connect (and
    # a scheduled healer retries any connection still NULL). NULL = never done.
    history_backfilled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Inverter(Base):
    """A persisted, owner-arrangeable inverter — the FIRST-CLASS unit behind the
    Array Operator sandbox.

    Why this table exists (the integration that makes the sandbox real): an owner
    does not think in vendor "sites". They think in the physical reality on their
    roofs — "the six inverters at Londonderry: two on the south barn, three on the
    field, one on the garage". The vendor's site grouping is an artifact of how the
    installer registered the hardware and often does NOT match that mental model.
    This table lets the owner REPRODUCE THEIR MODEL: drag an inverter and it really
    moves to the array they put it in, which changes its peer cohort, its reports,
    and its per-array billing rollup.

    Two concerns are kept deliberately separate:

      * TELEMETRY SOURCE (immutable) — where this inverter's data physically comes
        from. `vendor` + `source_site_id` + `serial` identify the exact equipment
        feed (e.g. SolarEdge site 416160, inverter SN 7F1C-...). You cannot re-wire
        which site a panel reports to from a web canvas, so these never change on a
        move. `source_connection_id` points at the InverterConnection we pull through.

      * OWNER GROUPING (mutable) — `array_id` is which Array the owner has placed
        this inverter under. THIS is what a drag edits. Seeded on discovery to the
        Array that owns the source site, then freely reassignable. `position`
        orders inverters within an array for a stable canvas layout.

    Discovery is idempotent: keyed by (tenant_id, vendor, serial). Re-running
    discovery updates nameplate/model/last_seen but NEVER clobbers the owner's
    array_id/position (their arrangement is sacred). Soft-delete via deleted_at so
    an inverter that drops off the vendor for a few days doesn't vanish from the
    owner's layout.
    """
    __tablename__ = "inverters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    # Owner grouping — the mutable bit a drag edits.
    array_id: Mapped[int] = mapped_column(Integer, ForeignKey("arrays.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Telemetry source — immutable origin of the data feed.
    vendor: Mapped[str] = mapped_column(String(20))                       # solaredge | locus | ...
    serial: Mapped[str] = mapped_column(String(128), index=True)          # vendor inverter SN
    source_site_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("inverter_connections.id"), nullable=True
    )
    # The Array whose connection originally surfaced this inverter — lets us pull
    # telemetry even after the owner regroups it under a different array, and lets
    # "reset layout" snap back to the discovered (source) grouping.
    source_array_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("arrays.id"), nullable=True
    )
    # Display / analysis metadata (refreshed on discovery).
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # True once the OWNER has renamed this inverter from the dashboard. Discovery
    # refreshes name/model/nameplate on every sync, but an owner-set name is part
    # of "their arrangement is sacred" — so when this is True the telemetry name
    # must NOT clobber it (same principle as never moving their array_id/position).
    name_is_custom: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    nameplate_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Last instantaneous AC power (W) and when it was observed. API-pulled vendors
    # (SolarEdge) are fetched live in build_fleet_tree so they never use these.
    # Extension-captured vendors (Fronius/SMA/Chint) have NO pullable feed, so the
    # browser ships the portal's live SITE power at capture time; we allocate it
    # per inverter by today's energy share and stamp it here. build_fleet_tree only
    # surfaces it while fresh (see _POWER_FRESH) so a stale capture ages back to
    # "—" instead of implying the panels are producing hours later / at night.
    last_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_power_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The SOURCE's own last-data timestamp (Fronius LastImport, SMA reading ts) — when
    # the inverter last reported to ITS vendor portal. Distinct from last_power_at (when
    # WE captured): a stale source_last_data_at means the data is frozen even if we keep
    # re-scraping it, so freshness + live-power gating keys off this when present.
    source_last_data_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "vendor", "serial", name="uq_inverter_tenant_vendor_serial"),
    )


class InverterDaily(Base):
    """One row per (inverter, calendar day) of generated kWh — the per-inverter
    analog of DailyGeneration (which is per-ARRAY).

    Why this exists: vendors we reach via the official API (SolarEdge) are
    pulled live in build_fleet_tree, so their per-inverter telemetry is fetched
    on demand and never needs persisting. But vendors captured through the
    EXTENSION (Fronius Solar.web — its Query API is paid and not sold in the USA)
    have NO connection the backend can pull through; the browser ships the
    readings once and they must be STORED. This table is that store: the fleet
    tree reads it as a telemetry fallback so a Fronius array shows its real
    per-inverter comb (peer-analyzed) exactly like an API-connected one.

    Keyed by (inverter_id, day) so a re-capture upserts in place. `kwh` is the
    day's energy for that single inverter (integrated from the portal's power
    curve at capture time).
    """
    __tablename__ = "inverter_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    inverter_id: Mapped[int] = mapped_column(Integer, ForeignKey("inverters.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kwh: Mapped[float] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="extension_pull")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    __table_args__ = (
        UniqueConstraint("inverter_id", "day", name="uq_inverter_daily_inv_day"),
    )


class InverterReading(Base):
    """High-frequency instantaneous power time-series — the data-hub's live memory.

    One row per (inverter, poll tick). The generalized server-side poller
    (poll_all_sources) writes a row every time it fetches a vendor we hold
    pullable API credentials for (SolarEdge today; SMA/Fronius/Locus/AlsoEnergy
    as their OAuth/app creds come online). This is what makes the product a
    real-time data hub instead of a stale-snapshot viewer: the fleet tree's
    "current kW" and the live sparkline read the NEWEST reading here, and the
    intraday power curve is the series of them.

    Distinct from InverterDaily (one kWh total per day) — this is sub-hourly
    instantaneous watts. Pruned on a rolling window (keep_days) so the table
    stays bounded; daily energy is rolled up into InverterDaily before prune.
    """
    __tablename__ = "inverter_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    inverter_id: Mapped[int] = mapped_column(Integer, ForeignKey("inverters.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy_today_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source: Mapped[str] = mapped_column(String(24), default="poll")

    __table_args__ = (
        Index("ix_inverter_readings_inv_ts", "inverter_id", "ts"),
    )


class WarrantyClaim(Base):
    """An automatic warranty / service claim — the paperwork arm of Array
    Operator. The agent watches the fleet and the MOMENT an inverter goes DEAD
    or throws a hardware FAULT (the two warrantable failures), it opens one of
    these rows: drafts the manufacturer email, snapshots the peer-measured
    evidence (so weather can't be blamed), and runs it through a lifecycle the
    owner controls.

    Lifecycle (`stage`):
        ready     — drafted, awaiting the owner's approval to file
        queued    — auto-send scheduled; `send_at` is when the grace timer fires
        sent      — filed with the manufacturer (or emailed to the owner to
                    forward — see warranty_claims.file_claim)
        resolved  — repaired/replaced; `recovered_usd` banked
        dismissed — owner waved it off (false alarm / handled elsewhere)
        cleared   — the inverter recovered on its own before we ever filed

    Evidence + draft are JSON snapshots captured at detection time so the claim
    stays faithful even after the inverter is regrouped, renamed, or drops off
    the vendor feed. The display columns (serial/inv_name/model/...) are likewise
    snapshots, not joins, for the same reason.

    Episode model: at most ONE active claim (stage in ready/queued/sent) per
    inverter. resolved/dismissed/cleared rows are history — if the same inverter
    fails again later, reconcile opens a fresh claim.
    """
    __tablename__ = "warranty_claims"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    array_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("arrays.id"), nullable=True, index=True)
    inverter_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("inverters.id"), nullable=True, index=True)

    # Display snapshots — kept even if the inverter later moves/disappears.
    serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    inv_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    nameplate_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    fail_type: Mapped[str] = mapped_column(String(12))                    # dead | fault
    stage: Mapped[str] = mapped_column(String(12), default="ready", index=True)
    send_mode: Mapped[str | None] = mapped_column(String(12), nullable=True)  # per-claim override of tenant default

    evidence: Mapped[dict] = mapped_column(JSON, default=dict)            # peer-measured snapshot
    draft: Mapped[dict] = mapped_column(JSON, default=dict)              # {to, subject, body}
    recovered_usd: Mapped[float] = mapped_column(Float, default=0.0)

    send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)     # queued → fire time
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_via: Mapped[str | None] = mapped_column(String(12), nullable=True)       # owner | auto
    sent_to: Mapped[str | None] = mapped_column(String(200), nullable=True)       # actual recipient
    sent_direct: Mapped[bool] = mapped_column(Boolean, default=False)             # True = straight to manufacturer
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    __table_args__ = (
        Index("ix_warranty_claims_tenant_stage", "tenant_id", "stage"),
    )


class VerificationCheck(Base):
    """Operator uploads their own records to compare against the SO-generated workbook."""
    __tablename__ = "verification_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"))
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"))
    array_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("arrays.id"), nullable=True)
    uploaded_filename: Mapped[str] = mapped_column(String(500))
    uploaded_mime: Mapped[str] = mapped_column(String(100))
    storage_path: Mapped[str] = mapped_column(String(1000))
    period_label: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | confirmed | flagged
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_verification_tenant", "tenant_id"),
        Index("ix_verification_client", "client_id"),
    )


class DiscoveredUtility(Base):
    """A SmartHub deployment seen in the wild that is not yet in the curated
    CSV catalog (api/data/providers/*.csv).

    Rows are minted automatically the first time any tenant's extension
    captures from an unknown *.smarthub.coop host (provider code "sh_<sub>").
    This is the fleet-learning loop: first login anywhere = the utility starts
    working immediately under its discovered code AND we get a signal to
    promote it to the catalog (one CSV line + registry regen) so the next
    operator gets a proper name in the UI.

    promoted_code is set when the utility graduates to the catalog; the
    promotion script backfills UtilityAccount.provider sh_* rows then.
    """
    __tablename__ = "discovered_utilities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    provider_code: Mapped[str] = mapped_column(String(40), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    capture_count: Mapped[int] = mapped_column(Integer, default=0)
    # Diagnostics from the wild: which capture layer worked (api | dom | usage)
    # and the last extension version seen — tells us whether new deployments
    # are drifting away from our parsers.
    last_capture_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_extension_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    promoted_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BillingReportSubscription(Base):
    """An Array Operator automatic-report schedule for ONE customer.

    Created from the Reports tab: the operator uploads a billing workbook, the
    matcher recognizes it, and this row remembers (a) the source workbook bytes
    so each cycle regenerates from the same source of truth, (b) the parsed
    field map, (c) the cadence + recipient slider + format choices.

    The scheduler (api/scheduler.py deliver_billing_reports) walks enabled rows
    whose cadence matches and emails the regenerated invoice + summary per
    send_mode. New rows default send_mode='to_me' so nothing reaches a real
    customer until the operator deliberately moves the slider — customer email
    is outward-facing and hard to recall.
    """
    __tablename__ = "billing_report_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    # The customer "underneath" the operator. Reuses the Client table; nullable
    # so a subscription can exist before a Client row is linked.
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clients.id"), nullable=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(200))

    # The uploaded workbook — the per-cycle source of truth (Railway disk is
    # ephemeral, so the bytes live in-row). + the parsed match snapshot.
    source_workbook: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    parsed_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    billing_model: Mapped[str] = mapped_column(String(24), default="percent_of_array")

    # Manual customer-input path (no workbook): the operator TYPES the customer
    # straight into the Reports tab — name, which array, their allocation share.
    # When source_workbook is NULL these two carry the truth instead of
    # parsed_map: delivery/draft compute the customer's share as
    # allocation_pct × the array's period generation. Both NULL for the
    # workbook-driven path, which keeps its allocation inside parsed_map.
    allocation_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..1
    array_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("arrays.id"), nullable=True, index=True)

    # OFFTAKER ↔ UTILITY BILL binding (Jun 2026, Ford's offtaker-report rule).
    # Offtaker invoices are computed EXCLUSIVELY from the utility's paper bills
    # (Bill.kwh_generated for THIS GMP account, per billing period) — never from
    # vendor/inverter telemetry, never from the GMP hourly-interval data, never
    # from a daily CSV. When this is set the delivery path reads utility-bill kWh
    # for this account ONLY and SKIPS (waits) if no bill covers the period, rather
    # than falling back to any other source. Nullable for back-compat with older
    # array-based subscriptions; new offtakers select the GMP bill that's theirs.
    utility_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("utility_accounts.id"), nullable=True, index=True)

    # Multi-array allocations (Jun 2026): an offtaker can own a share of SEVERAL
    # arrays at once. List of {"array_id": int, "allocation_pct": float 0..1}.
    # When set + non-empty, delivery SUMS each array's (period kWh × pct) into one
    # combined invoice (one line per array). When NULL/empty, the legacy single
    # array_id/allocation_pct path above drives delivery unchanged (back-compat).
    array_allocations: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Per-customer billing rate override ($/kWh, Jun 2026). When set, this
    # customer's invoice is priced at this rate; when NULL, pricing falls back
    # to the operator's global Tenant.default_billing_rate_per_kwh, then to the
    # legacy VT default. Lets Paul set one global rate and override per offtaker.
    rate_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Per-customer discount-model overrides (Jun 2026). When set, they win over
    # the tenant's global defaults. invoice = kWh × net_rate × (1 − discount).
    #   discount_pct      — fraction in [0,1); null → operator global → 10% default
    #   net_rate_per_kwh  — $/kWh the discount applies to; null → global → VT default
    discount_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rate_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Dormant hook (Paul's reporting build): the utility's GMP invoice PDF, fed
    # later by the GMP-detection backend. When present, delivery attaches it
    # alongside the customer invoice; null (the norm today) changes nothing.
    gmp_invoice_pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Auto-attach the captured GMP bill PDF (Jun 2026). When True, delivery looks
    # up the matching GMP bill PDF for this customer's array + billing period via
    # the read seam (api/reports/gmp_bill_pdf_read) and attaches it automatically
    # — Paul never hand-uploads. When no PDF is captured yet, nothing is attached
    # (never fabricated). ON by default; a captured bill auto-attaches once found.
    auto_attach_gmp: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False)

    # BRING-YOUR-OWN generation spreadsheet (Jun 2026, Ford). The operator
    # uploads their OWN ongoing generation-tracking sheet (whatever columns they
    # already use); a heuristic detector (api/billing/sheet_tracker.py) maps its
    # columns and we APPEND a new row each month as a fresh GMP bill lands —
    # preserving their layout. A "Download latest spreadsheet" button on the
    # invoice page streams the kept-current file. Distinct from source_workbook
    # (which is the HCT billing-template the invoice is GENERATED from); this is
    # the offtaker's personal running ledger that we keep current.
    #   tracker_workbook — the live xlsx bytes (CSV uploads are normalized to xlsx)
    #   tracker_filename — original upload name (for the download filename)
    #   tracker_map      — the detected structure (sheet/header_row/columns/…) +
    #                      the idempotency cursor (last_period). NULL = no sheet.
    tracker_workbook: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    tracker_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    tracker_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tracker_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Schedule
    cadence: Mapped[str] = mapped_column(String(16), default="monthly")  # monthly | quarterly
    annual_trueup: Mapped[bool] = mapped_column(Boolean, default=False)

    # When a scheduled period comes due, how to handle it:
    #   "approval" (default) → DRAFT it into the approval inbox and email the
    #       operator a "ready to review" note; nothing reaches the customer until
    #       the operator opens it, optionally edits, and clicks Approve & send.
    #   "auto" → send straight to the recipient per send_mode (the original
    #       hands-off behavior). Both modes are offered per customer.
    delivery_mode: Mapped[str] = mapped_column(String(12), default="approval")

    # Recipient slider (Ford: to me / to my client / to both)
    send_mode: Mapped[str] = mapped_column(String(20), default="to_me")
    # to_me | to_client | to_both
    client_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cc_emails: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Format choices (Ford: "you should be able to choose")
    formats: Mapped[dict | None] = mapped_column(JSON, default=lambda: ["pdf"])
    # The Array Operator performance summary is OPT-IN (Ford 2026-06-24): off by
    # default so it never auto-attaches to an offtaker invoice unless the operator
    # ticks "Attach Array Operator's summary data" on the draft card.
    include_summary: Mapped[bool] = mapped_column(Boolean, default=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_invoice_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # "Come review your next bill" dedup (Jun 2026, Ford's GMP-update trigger).
    # The latest GMP-bill PERIOD label (YYYY-MM of the bill's period_end) for
    # which api/jobs/new_bill_review already emailed the operator a "your next
    # invoice is ready to review" prompt. The daily sweep only fires when a NEWER
    # bill period lands than the one stored here, so each new GMP bill triggers
    # exactly one review email per offtaker. NULL = never review-emailed yet.
    review_emailed_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Sequential invoice numbering (Ford): the operator sets a starting number and
    # Array Operator adds 1 per real send. `start` = the seed they entered; `next` =
    # the running counter stamped on the next invoice. NULL on both = legacy
    # period-date number ("2026-06").
    invoice_number_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invoice_number_next: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Budget billing (Paul): a per-offtaker FIXED final amount the operator enters
    # that OVERRIDES the calculated Amount Due. All the line items still compute and
    # show on the invoice; only the total becomes this number. NULL = use the
    # calculated amount.
    budget_amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        Index("ix_billing_sub_tenant_enabled", "tenant_id", "enabled"),
    )


class ReportDraft(Base):
    """A drafted billing report awaiting the operator's approval before it's sent.

    Paul Bozuwa's core ask: when a billing period is ready, the system should
    DRAFT the customer's invoice email (customer invoice PDF + the GMP utility
    invoice PDF) and drop it in an approval inbox — "I get an email drafted...
    I go over it and approve it or modify it and then send." Nothing reaches a
    real customer until the operator clicks Approve & send.

    A draft snapshots the numbers at generation time (period, total array kWh,
    the customer's allocation %, the customer's share, amount) so the inbox can
    render the card without recomputing, and carries the optional GMP invoice PDF
    the operator attaches. Approving calls the normal deliver_subscription path
    (which already attaches sub.gmp_invoice_pdf), so the email/format/recipient
    machinery is reused — the draft is the human gate in front of it.
    """
    __tablename__ = "report_drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    subscription_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("billing_report_subscriptions.id"), index=True)
    customer_name: Mapped[str] = mapped_column(String(200))

    # pending → sent | dismissed. Only one pending draft per subscription/period.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    # Snapshot of the numbers the draft was built from (for the inbox card).
    period_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    array_total_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    allocation_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..1
    customer_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # The operator can attach the period's GMP utility invoice PDF (Paul sends it
    # alongside the customer invoice "to prove we're not just making this up").
    gmp_invoice_pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    gmp_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Editable-before-send overrides (Paul: "approve it or MODIFY it and send").
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_report_drafts_tenant_status", "tenant_id", "status"),
    )


class OfftakerInvoiceTemplate(Base):
    """The operator's OWN invoice template, uploaded so generated offtaker invoices
    can reproduce THEIR exact format (Array Operator, Jun 2026).

    Stage 1 (this row): store the uploaded original (`file_bytes` + `filename`) and
    an optional editable token-HTML rendition (`html`), keyed one-per-tenant. The
    render-from-template path that actually emits invoices in this format is gated
    behind `enabled` (Stage 2) so a half-built template can never reach a real
    offtaker; until then offtaker invoices keep using the standard branded PDF.
    """
    __tablename__ = "offtaker_invoice_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"),
                                           index=True, unique=True)
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Editable token-HTML rendition (Jinja2 placeholders) — the render source in Stage 2.
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gate: only when True does invoice generation render from this template.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class OfftakerSubscriptionTemplate(Base):
    """A PER-OFFTAKER invoice template — one offtaker's own uploaded format, keyed by
    subscription. Same shape as OfftakerInvoiceTemplate (the tenant-wide default) but
    scoped to a single offtaker; it OVERRIDES the tenant default at render time (see
    delivery._effective_template_row). `enabled` gates rendering from it just like the
    tenant row, so a half-built per-offtaker template can never reach a real send.

    New table → auto-created by init_db()/create_all on deploy (no manual migration).
    """
    __tablename__ = "offtaker_subscription_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("billing_report_subscriptions.id"), index=True, unique=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Editable token-HTML rendition (Jinja2 placeholders) — the render source.
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gate: only when True does invoice generation render from this template.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class CaptureEvent(Base):
    """One stage in a capture pipeline run (/v1/sync).

    capture_id (UUID4) groups all events from the same sync call so they
    can be displayed as a single timeline entry in the dev panel.

    stage values: ingest_received | client_created | client_matched |
                  client_merged | array_created | array_skipped | capture_error
    payload_excerpt is scrubbed via capture_events._safe_excerpt before insert;
    auth tokens and provider blob data are never stored.
    """
    __tablename__ = "capture_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    capture_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_excerpt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    __table_args__ = (
        Index("ix_capture_events_tenant_created", "tenant_id", "created_at"),
    )
