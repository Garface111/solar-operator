"""Per-login utility-session selection.

A `UtilitySession` is the captured auth (JWT/token) for one utility *login*. An
operator may log into MULTIPLE distinct utility customers — e.g. a separate GMP
login per client — and each of those logins must persist and stay independently
usable for ongoing scraping, not just the most recently captured one.

To make that work, every session is tagged with the login's identity
(`customer_number`, the GMP personId / SmartHub custNbr shared by that login's
accounts), and bill/generation pulls pick the token bound to the *account's own*
customer_number. If an account has no customer_number (legacy rows, or a
provider that doesn't expose one) or no identity-matched session exists yet, we
fall back to the latest session for the provider — so nothing regresses.

This is the fix for the "latest login clobbers all previous logins" bug: before,
selection was `latest session per (tenant, provider)`, so logging in as client B
made client A's accounts un-scrapeable.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from .models import UtilitySession, UtilityAccount


def latest_session(db, tenant_id: str, provider: str) -> Optional[UtilitySession]:
    """The most-recently-captured session for a provider (legacy fallback)."""
    return db.execute(
        select(UtilitySession)
        .where(UtilitySession.tenant_id == tenant_id,
               UtilitySession.provider == provider)
        .order_by(UtilitySession.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def session_for_account(db, account: UtilityAccount) -> Optional[UtilitySession]:
    """The session whose login can actually read THIS account.

    Prefers the session captured for the account's own customer_number (its
    login identity); falls back to the latest session for the provider when the
    account has no customer_number or no identity-matched session exists.
    """
    cust = getattr(account, "customer_number", None)
    if cust:
        sess = db.execute(
            select(UtilitySession)
            .where(UtilitySession.tenant_id == account.tenant_id,
                   UtilitySession.provider == account.provider,
                   UtilitySession.customer_number == cust)
            .order_by(UtilitySession.captured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if sess is not None:
            return sess
    return latest_session(db, account.tenant_id, account.provider)


def token_for_account(db, account: UtilityAccount) -> Optional[str]:
    """The auth token to use when pulling data for `account`, or None."""
    sess = session_for_account(db, account)
    return sess.api_token if sess else None


def session_customer_number(accounts: list[dict]) -> Optional[str]:
    """The login identity for a capture: the single customer_number shared by
    its accounts.

    Returns that customer_number when every account in the capture agrees on one
    (the normal GMP/SmartHub case — one login → one customer → many accounts).
    Returns None when the capture exposes zero or multiple distinct
    customer_numbers, so the caller stores the session unkeyed and selection
    falls back to legacy 'latest per provider'.
    """
    nums = {str(a.get("customer_number")) for a in accounts if a.get("customer_number")}
    return next(iter(nums)) if len(nums) == 1 else None
