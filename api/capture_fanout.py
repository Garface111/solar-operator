"""Fan a single extension capture out into a user's LINKED sibling tenant.

"One extension install feeds BOTH products." The extension stores one tenant_key
→ one tenant → one product. When that tenant is cross-product LINKED
(api.tenant_link sets Tenant.linked_tenant_id bidirectionally), the SAME captured
payload that landed for the primary product is replayed into the sibling tenant,
so a single install feeds both the NEPOOL reports side and the AO monitoring side.

Safety contract (the bar):
  • FEATURE-FLAGGED, default OFF. With FAN_OUT_TO_SIBLING unset/falsy, fanout()
    is a no-op — real captures are byte-for-byte unchanged until deliberately
    enabled. Nothing about linking alone changes data flow; only this flag does.
  • The sibling write runs the SAME endpoint write-logic as the primary (the
    caller passes the very function it just ran), so the sibling gets the
    identical dup-safe treatment: _safe_create_array SAVEPOINTs, upsert-daily
    max-wins, per-site flush, plausibility guards. No logic is duplicated → no
    drift, no chance of a second, less-careful write path corrupting the dup
    constraints (uq_array_per_tenant, uq_daily_array_day, uq_inverter_*).
  • The primary ALWAYS succeeds. The sibling replay is wrapped — any exception is
    logged and swallowed; it can never fail or roll back the primary capture
    (which has already committed by the time fanout runs).
  • Mirror only what the sibling product can use; the sibling's arrays/daily come
    ONLY from the real captured payload — nothing is fabricated.
"""
from __future__ import annotations

import logging
import os

from .db import SessionLocal
from .models import Tenant
from .tenant_link import get_linked_sibling

log = logging.getLogger(__name__)


def fanout_enabled() -> bool:
    """True iff the FAN_OUT_TO_SIBLING env flag is set truthy. Read at call time
    (not import) so flipping the Railway var takes effect on the next request
    without a code deploy. Default OFF."""
    return os.getenv("FAN_OUT_TO_SIBLING", "").strip().lower() in ("1", "true", "yes", "on")


def linked_sibling_id(primary: Tenant) -> str | None:
    """Resolve the validated sibling tenant id for a primary tenant, or None.

    Re-opens a short session and re-validates the link (exists, points back,
    different product) so a dangling/half-removed link never misroutes. Returns
    just the id — the caller re-resolves the Tenant inside its own session so the
    sibling write uses a fresh, correctly-scoped row."""
    if not getattr(primary, "linked_tenant_id", None):
        return None
    with SessionLocal() as db:
        sib = get_linked_sibling(db, primary)
        return sib.id if sib is not None else None


def fanout(primary: Tenant, replay) -> dict:
    """Replay a just-committed capture into the primary's linked sibling.

    `replay` is a callable taking ONE argument — the sibling Tenant — that runs
    the exact same write-logic the public endpoint ran for the primary. The
    caller is responsible for re-fetching the sibling inside `replay`'s own DB
    session (we pass the Tenant; the endpoint's `_with_tenant` body opens its own
    SessionLocal, identical to the primary path).

    Returns a small status dict for observability; never raises.
    """
    result = {"attempted": False, "sibling_id": None, "ok": False, "error": None}
    try:
        if not fanout_enabled():
            return result
        sib_id = linked_sibling_id(primary)
        if not sib_id:
            return result
        result["attempted"] = True
        result["sibling_id"] = sib_id
        # Re-fetch the sibling as a detached, freshly-loaded Tenant so the replay
        # body binds it into its own session exactly like the primary capture did.
        with SessionLocal() as db:
            sib = db.get(Tenant, sib_id)
            if sib is None:
                result["error"] = "sibling-vanished"
                return result
            # Snapshot the fields the replay path reads (id + product) so it can
            # operate without holding this session open.
            sib_snapshot = sib
            db.expunge(sib_snapshot)
        replay(sib_snapshot)
        result["ok"] = True
        log.info("capture-fanout: replayed capture from %s into linked sibling %s",
                 primary.id, sib_id)
    except Exception:  # noqa: BLE001 — sibling fan-out must NEVER break the primary
        result["error"] = "exception"
        log.warning("capture-fanout: sibling replay failed for primary %s "
                    "(primary capture already committed, unaffected)",
                    primary.id, exc_info=True)
    return result
