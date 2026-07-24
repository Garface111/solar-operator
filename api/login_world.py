"""Resurrect a login's known world when a client is created from that login.

Ford (2026-07-24): "if we already have the login in the system, the moment they
add it as a client it should be preloaded and ready to go." He deleted Pbozuwa,
re-added it from the saved VEC login, and got an empty client with "0 of 0
arrays ready" — while the login's two arrays, both utility accounts, and 635
days of generation history sat soft-deleted one flag away, waiting for a
harvester pass that might be hours out.

So: creating a client that carries a gmp/vec login identity now SYNCHRONOUSLY
re-attaches the most recent deleted same-login client's world — its arrays
(un-deleted, re-pointed) and their utility accounts — inside the caller's
transaction. History (DailyGeneration) never left, so reports are ready the
moment the create returns.

Scope guards:
  • Live clients are untouchable — the create path's login-already-claimed 409
    fires before this ever runs, so the only reachable donor is a DELETED one.
  • A live array that re-took a name keeps it; the ghost stays dead (it is
    almost certainly the same physical array living under another client —
    resurrecting it would double-count generation).
  • Nearest predecessor wins: we take ONE deleted client's world, not a merge
    of every generation of deletions.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, or_, select

from .models import Array, Client, UtilityAccount

log = logging.getLogger(__name__)


def attach_prior_login_world(db, tenant_id: str, c: Client) -> int:
    """Re-attach the newest deleted same-login client's arrays + accounts to
    `c`, inside the caller's transaction (no commit here). Returns the number
    of arrays attached; 0 when the client carries no login or nothing is known.
    """
    conds = []
    for col, val in (
        (Client.gmp_email, c.gmp_email),
        (Client.gmp_username, c.gmp_username),
        (Client.vec_email, c.vec_email),
        (Client.vec_username, c.vec_username),
    ):
        if val and str(val).strip():
            conds.append(func.lower(col) == str(val).strip().lower())
    if not conds:
        return 0

    donors = db.execute(
        select(Client).where(
            Client.tenant_id == tenant_id,
            Client.id != c.id,
            Client.deleted_at.is_not(None),
            or_(*conds),
        ).order_by(Client.deleted_at.desc())
    ).scalars().all()
    if not donors:
        return 0

    live_names = {
        (n or "").strip().lower()
        for (n,) in db.execute(
            select(Array.name).where(
                Array.tenant_id == tenant_id, Array.deleted_at.is_(None)
            )
        ).all()
    }

    attached = 0
    for donor in donors:
        ghosts = db.execute(
            select(Array).where(
                Array.client_id == donor.id,
                Array.deleted_at.is_not(None),
            )
        ).scalars().all()
        for arr in ghosts:
            key = (arr.name or "").strip().lower()
            if key in live_names:
                continue  # a live array owns this name — never double-count
            arr.client_id = c.id
            arr.deleted_at = None
            live_names.add(key)
            attached += 1
            for ua in db.execute(
                select(UtilityAccount).where(UtilityAccount.array_id == arr.id)
            ).scalars().all():
                ua.deleted_at = None
        if attached:
            log.info(
                "login-world: restored %d array(s) from deleted client %s "
                "onto client %s (tenant %s)", attached, donor.id, c.id, tenant_id,
            )
            break  # nearest predecessor wins
    return attached
