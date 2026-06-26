"""Cross-product tenant linking — "one extension install feeds BOTH products".

A person can own a NEPOOL tenant AND an Array Operator tenant on the same email.
Today they are two SEPARATE `tenants` rows with different `product` and NO link,
so the browser extension (which stores ONE tenant_key) captures into exactly one
of them. This module establishes a deliberate, verified-email-scoped LINK between
a user's two product tenants via the nullable self-referential
`Tenant.linked_tenant_id`, so a capture into one can fan out into the other (see
api.capture_fanout — the fan-out itself is feature-flagged, default OFF).

Design rules (the bar):
  • OPT-IN: nothing is linked automatically. A guarded admin endpoint / CLI
    script calls link_by_email() for ONE email at a time. There is NO blind
    whole-DB sweep.
  • VERIFIED-EMAIL-SCOPED: only two tenants that share the SAME normalized
    contact_email AND have DIFFERENT products are linked.
  • CANONICAL per product: when an email has duplicate tenants for a product
    (Bruce's near-dup rows), we resolve to ONE canonical, NON-dup tenant per
    product — preferring active, then a real subscription, then most data,
    then oldest — and link THOSE. We never link to a stale duplicate.
  • BIDIRECTIONAL: both rows point at each other.
  • REVERSIBLE: unlink_tenant() nulls both sides. The FK is loose (no DB
    constraint) so nulling never trips referential integrity.

This module does the SELECTION + the link/unlink writes only. It never touches
captured data and never enables fan-out.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select

from .db import SessionLocal
from .models import Array, DailyGeneration, Inverter, Tenant

log = logging.getLogger(__name__)


def normalize_email(email: str | None) -> str:
    """The repo-wide email-normalization convention (account.py uses .lower().strip())."""
    return (email or "").lower().strip()


def _is_recoverable(t: Tenant) -> bool:
    """A tenant that is active OR in a recoverable (paused/trial/comped) state —
    i.e. a real, live customer relationship, not a hard-dead/abandoned row.
    Mirrors array_owners._CAPTURE_RECOVERABLE_STATUSES."""
    return bool(t.active) or (t.subscription_status in
                              {"paused_no_card", "trialing", "comped", "active", None})


def _data_weight(db, t: Tenant) -> int:
    """How much real captured data a tenant carries — used to break ties between
    duplicate tenants for the same product, so the canonical pick is the one the
    user actually USES, not an empty stale clone."""
    arrays = db.execute(
        select(func.count(Array.id)).where(
            Array.tenant_id == t.id, Array.deleted_at.is_(None))
    ).scalar() or 0
    invs = db.execute(
        select(func.count(Inverter.id)).where(
            Inverter.tenant_id == t.id, Inverter.deleted_at.is_(None))
    ).scalar() or 0
    days = db.execute(
        select(func.count(DailyGeneration.id)).where(
            DailyGeneration.tenant_id == t.id)
    ).scalar() or 0
    return int(arrays) * 1000 + int(invs) * 100 + int(days)


def _canonical_score(db, t: Tenant) -> tuple:
    """Sort key (higher = MORE canonical) for picking the one real tenant per
    product among possible duplicates. Preference order:
      1. live/recoverable over hard-dead
      2. has a real Stripe subscription over none
      3. carries more real captured data
      4. older row (created first — the original, not a re-signup clone)
    """
    has_sub = bool(t.stripe_subscription_id)
    weight = _data_weight(db, t)
    # created_at ascending → negate via ordinal so "older wins" sorts higher.
    # Use a large constant minus epoch seconds so older (smaller ts) → larger key.
    try:
        age_key = -t.created_at.timestamp()
    except Exception:
        age_key = 0.0
    return (1 if _is_recoverable(t) else 0, 1 if has_sub else 0, weight, age_key)


def canonical_tenant_for_product(db, email_norm: str, product: str) -> Optional[Tenant]:
    """Return the ONE canonical (active/non-dup) tenant for (email, product), or
    None if the email owns no tenant of that product. When duplicates exist, the
    most-canonical row wins per _canonical_score — a stale duplicate is never
    returned."""
    rows = db.execute(
        select(Tenant).where(
            func.lower(func.trim(Tenant.contact_email)) == email_norm,
            Tenant.product == product,
        )
    ).scalars().all()
    if not rows:
        return None
    return max(rows, key=lambda t: _canonical_score(db, t))


# The two products that can be linked. A link is ALWAYS between one of each.
_LINKABLE_PRODUCTS = ("nepool", "array_operator")


def resolve_link_pair(db, email_norm: str) -> tuple[Optional[Tenant], Optional[Tenant]]:
    """Resolve the canonical (nepool_tenant, array_operator_tenant) pair for an
    email. Either may be None if the email owns no tenant of that product."""
    nep = canonical_tenant_for_product(db, email_norm, "nepool")
    ao = canonical_tenant_for_product(db, email_norm, "array_operator")
    return nep, ao


def link_by_email(email: str, *, apply: bool = False) -> dict:
    """Establish (or preview) the cross-product link for ONE email.

    Resolves the canonical NEPOOL tenant and the canonical Array Operator tenant
    sharing this email and, when both exist and have DIFFERENT products, sets
    `linked_tenant_id` bidirectionally.

    apply=False (default) → DRY RUN: report what WOULD be linked, write nothing.
    apply=True            → commit the bidirectional link.

    Returns a structured result describing the resolution + action taken. Never
    raises on "nothing to link" — that's a normal, reported outcome.
    """
    email_norm = normalize_email(email)
    out: dict = {
        "email": email_norm,
        "apply": apply,
        "linked": False,
        "reason": None,
        "nepool_tenant": None,
        "array_operator_tenant": None,
    }
    if not email_norm:
        out["reason"] = "empty-email"
        return out

    with SessionLocal() as db:
        nep, ao = resolve_link_pair(db, email_norm)
        out["nepool_tenant"] = _describe(db, nep)
        out["array_operator_tenant"] = _describe(db, ao)

        if nep is None or ao is None:
            out["reason"] = "missing-one-product"  # email lacks one of the two
            return out
        if nep.id == ao.id:
            out["reason"] = "same-tenant"  # defensive; products differ so impossible
            return out

        # Idempotent: already linked to each other → no-op success.
        if nep.linked_tenant_id == ao.id and ao.linked_tenant_id == nep.id:
            out["linked"] = True
            out["reason"] = "already-linked"
            return out

        if not apply:
            out["reason"] = "dry-run-would-link"
            return out

        nep.linked_tenant_id = ao.id
        ao.linked_tenant_id = nep.id
        db.commit()
        out["linked"] = True
        out["reason"] = "linked"
        log.info("tenant_link: linked %s (nepool) <-> %s (array_operator) for %s",
                 nep.id, ao.id, email_norm)
        return out


def unlink_tenant(tenant_id: str, *, apply: bool = False) -> dict:
    """Reverse a link: null `linked_tenant_id` on the given tenant AND its
    sibling (if the sibling still points back). apply=False previews."""
    out: dict = {"tenant_id": tenant_id, "apply": apply, "unlinked": False, "sibling": None}
    with SessionLocal() as db:
        t = db.get(Tenant, tenant_id)
        if t is None:
            out["reason"] = "no-such-tenant"
            return out
        sib_id = t.linked_tenant_id
        out["sibling"] = sib_id
        if not sib_id:
            out["reason"] = "not-linked"
            return out
        if not apply:
            out["reason"] = "dry-run-would-unlink"
            return out
        sib = db.get(Tenant, sib_id)
        t.linked_tenant_id = None
        if sib is not None and sib.linked_tenant_id == t.id:
            sib.linked_tenant_id = None
        db.commit()
        out["unlinked"] = True
        out["reason"] = "unlinked"
        log.info("tenant_link: unlinked %s <-> %s", tenant_id, sib_id)
        return out


def get_linked_sibling(db, tenant: Tenant) -> Optional[Tenant]:
    """Return the live, validated sibling tenant for a linked tenant, or None.

    Re-validates on every call (the link FK is intentionally loose): the sibling
    must (a) still exist, (b) point BACK at this tenant, and (c) be a DIFFERENT
    product. Any failure → None (the fan-out simply doesn't fire), so a dangling
    or half-removed link can never misroute a capture into a wrong/garbage row.
    """
    sib_id = getattr(tenant, "linked_tenant_id", None)
    if not sib_id:
        return None
    sib = db.get(Tenant, sib_id)
    if sib is None:
        return None
    if sib.linked_tenant_id != tenant.id:
        return None  # one-sided / stale link — refuse
    if (sib.product or "") == (tenant.product or ""):
        return None  # must be the OTHER product
    return sib


def _describe(db, t: Optional[Tenant]) -> Optional[dict]:
    if t is None:
        return None
    return {
        "id": t.id,
        "product": t.product,
        "company_name": t.company_name or t.name,
        "active": bool(t.active),
        "subscription_status": t.subscription_status,
        "has_subscription": bool(t.stripe_subscription_id),
        "data_weight": _data_weight(db, t),
        "linked_tenant_id": t.linked_tenant_id,
    }
