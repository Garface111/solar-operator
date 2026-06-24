"""
GMP bill-PDF READ SEAM  —  the contract the Reports agent consumes to AUTO-ATTACH
the utility's actual GMP bill PDF to a customer's invoice email.

╔══════════════════════════════════════════════════════════════════════════╗
║  CONSUMER-SIDE CONTRACT (v0).                                              ║
║  The Reports agent (this side) is a READ-ONLY consumer. It calls           ║
║  get_bill_pdf_for_period() to fetch the captured GMP bill PDF for an        ║
║  array + billing period and attaches it automatically (the per-customer    ║
║  BillingReportSubscription.auto_attach_gmp toggle). It NEVER pulls from     ║
║  GMP, scrapes, or writes — that capture is the ingestion/extension agent's  ║
║  job.                                                                       ║
╚══════════════════════════════════════════════════════════════════════════╝

OWNERSHIP / LANE
  • Pulling the bill PDF off GMP (the extension scrapes bill rows with direct
    bill-PDF URLs; api/adapters/gmp.fetch_bill_pdf downloads the bytes) and
    PERSISTING those bytes durably is the INGESTION agent's job.
  • This module only READS the persisted bytes and hands them to delivery.

WHAT THE INGESTION AGENT MUST PROVIDE (the gap this contract names)
  Today the extension captures a bill row per (account, period) including a
  `pdf_url`, and `Bill` stores `pdf_path` — but `pdf_path` points at Railway's
  EPHEMERAL disk, so it is NOT durable and cannot be relied on for attachment.
  To make auto-attach light up, ingestion must persist the actual PDF BYTES
  durably (in-row, like BillingReportSubscription.gmp_invoice_pdf) keyed by
  (utility_account_id, billing period). Suggested shape (ingestion owns it):

      Bill.pdf_bytes : LargeBinary | None   # the verbatim bill PDF
      Bill.pdf_content_type : str | None    # "application/pdf"

  Until that lands, get_bill_pdf_for_period() returns None and auto-attach
  simply attaches nothing (surfaced honestly in the UI; never fabricated).

GUARANTEES TO THE CONSUMER
  • Returns real captured bytes or None — never a placeholder/fabricated PDF.
  • Tenant/array scoping is the caller's responsibility (it passes an array_id
    it already owns); this module filters bills to that array's GMP accounts.
  • Read-only: SELECT only, no writes, no network.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Bill, UtilityAccount

logger = logging.getLogger(__name__)


def _gmp_account_ids_for_array(db: Session, array_id: int) -> list[int]:
    """The enabled GMP utility-account IDs feeding one array (1..N meters)."""
    return list(db.execute(
        select(UtilityAccount.id).where(
            UtilityAccount.array_id == array_id,
            UtilityAccount.provider == "gmp",
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all())


def _bill_pdf_bytes(bill: Bill) -> Optional[bytes]:
    """Durable PDF bytes for a bill, or None.

    Reads the durable in-row bytes the ingestion agent persists (see contract
    header). `pdf_path` is intentionally IGNORED — it points at ephemeral disk
    and cannot be trusted for attachment. Defensive: tolerates the column not
    existing yet (provisional ingestion side).
    """
    data = getattr(bill, "pdf_bytes", None)
    if data:
        return bytes(data)
    return None


def _pick_bill_pdf(
    db: Session,
    acct_ids: list[int],
    period_start: Optional[date],
    period_end: Optional[date],
) -> Optional[dict[str, Any]]:
    """The captured PDF for the newest bill (within [period_start, period_end]
    when given) across `acct_ids`, or None. Shared by the array- and account-
    keyed entry points below."""
    if not acct_ids:
        return None
    q = select(Bill).where(Bill.account_id.in_(acct_ids))
    # Order newest-first so the latest matching bill wins; the period filter
    # below narrows to the requested window when one is given.
    q = q.order_by(Bill.period_end.desc().nullslast(), Bill.bill_date.desc().nullslast())
    for bill in db.execute(q).scalars().all():
        # Period filter (inclusive overlap) when a window is given.
        if period_start is not None and bill.period_end is not None:
            if bill.period_end.date() < period_start:
                continue
        if period_end is not None and bill.period_start is not None:
            if bill.period_start.date() > period_end:
                continue
        data = _bill_pdf_bytes(bill)
        if data:
            ps = bill.period_start.date() if bill.period_start else None
            pe = bill.period_end.date() if bill.period_end else None
            label = pe.strftime("%Y-%m") if pe else "period"
            return {
                "bytes": data,
                "filename": f"GMP_bill_{label}.pdf",
                "content_type": getattr(bill, "pdf_content_type", None) or "application/pdf",
                "account_id": bill.account_id,
                "period_start": ps,
                "period_end": pe,
            }
    return None


def get_bill_pdf_for_period(
    array_id: int,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    *,
    db: Optional[Session] = None,
) -> Optional[dict[str, Any]]:
    """The captured GMP bill PDF for an array's billing period, or None.

    Matches the bill whose period overlaps [period_start, period_end] (when
    given) for any of the array's GMP accounts; otherwise the most recent bill.
    Returns:
        {"bytes": <pdf bytes>, "filename": str, "content_type": "application/pdf",
         "account_id": int, "period_start": date|None, "period_end": date|None}
    or None when no DURABLE PDF is captured yet (the norm until ingestion lands
    persisted bytes). Never returns a fabricated/placeholder PDF.

    Prefer get_bill_pdf_for_account() when the offtaker is bound to a SPECIFIC
    GMP account — that returns the exact bill the invoice was computed from,
    rather than whichever of the array's accounts has the newest captured PDF.
    """
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        return _pick_bill_pdf(db, _gmp_account_ids_for_array(db, array_id),
                              period_start, period_end)
    except Exception:  # noqa: BLE001 — provisional ingestion side / missing column
        logger.warning("GMP bill-PDF read failed for array %s", array_id, exc_info=True)
        return None
    finally:
        if _own:
            db.close()


def get_bill_pdf_for_account(
    utility_account_id: int,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    *,
    db: Optional[Session] = None,
) -> Optional[dict[str, Any]]:
    """The captured GMP bill PDF for ONE specific utility account, or None.

    This is the offtaker-correct lookup: an offtaker invoice is computed from the
    bill of the account it's BOUND to (BillingReportSubscription.utility_account_id),
    so the auto-attached PDF must come from that SAME account — not whichever of the
    array's sibling accounts happens to have the newest captured bill. Same return
    shape as get_bill_pdf_for_period(); None when the account isn't a live GMP
    account or has no durable PDF captured for the window.
    """
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        ua = db.get(UtilityAccount, utility_account_id)
        if ua is None or ua.provider != "gmp" or ua.deleted_at is not None:
            return None
        return _pick_bill_pdf(db, [utility_account_id], period_start, period_end)
    except Exception:  # noqa: BLE001 — provisional ingestion side / missing column
        logger.warning("GMP bill-PDF read failed for account %s", utility_account_id, exc_info=True)
        return None
    finally:
        if _own:
            db.close()


def has_capturable_gmp_account(array_id: int, *, db: Optional[Session] = None) -> bool:
    """True if the array has at least one GMP account (so auto-attach is
    MEANINGFUL even before any PDF is captured — lets the UI say 'will attach
    automatically once captured' vs 'no GMP account on this array')."""
    _own = db is None
    if _own:
        db = SessionLocal()
    try:
        return bool(_gmp_account_ids_for_array(db, array_id))
    finally:
        if _own:
            db.close()
