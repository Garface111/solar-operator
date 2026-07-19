"""Which arrays belong to the GENERATION-REPORTS world (THE FOLD).

THE RULE, in one line
---------------------
Generation reports are built from **utility data** — metered kWh off a real
utility account (bills + GMP interval data). They are NEVER built from vendor
telemetry (SolarEdge / Fronius / SMA / Chint inverter feeds). So an array that
exists ONLY because an inverter vendor reported it — a "vendor twin" — must
not appear anywhere in the generation-reports surface: not in the client roster
the operator sees, not in the NEPOOL-GIS nudge, not in a workbook, and above
all not in the $15/array/quarter billing unit.

THE DISCRIMINATOR (verified against live prod data, 2026-07-19)
---------------------------------------------------------------
An array is **vendor-only** iff:

    ZERO live UtilityAccount rows (deleted_at IS NULL)  AND  >=1 Inverter row

Both halves are load-bearing. Real proof from prod — the same tenant, the same
client, two arrays named for the same physical site:

    [2333] "Waterford"            2 utility accts, 6 inverters,
                                  gen sources {solaredge, bill_prorate},
                                  nepool_gis_id 12231     -> utility-backed, KEEP
    [2735] "Waterford (Fronius)"  0 utility accts, 12 inverters,
                                  gen source {extension_pull},
                                  nepool_gis_id None      -> vendor twin, EXCLUDE

Note what this rule does NOT say:

  * "has inverters" is NOT disqualifying. Array 2333 has six of them and is a
    perfectly legitimate reported array — the operator monitors hardware on a
    site whose kWh still comes off the meter. A naive "any inverters -> hide"
    rule deletes real money from the report.
  * an array with 0 accounts AND 0 inverters is KEPT. That is a freshly
    hand-added array: the operator typed a name and is about to link its
    utility account. Hiding it would make the linking flow impossible.

So the predicate is deliberately narrow: it only fires on arrays that have no
utility identity at all *and* demonstrably came from a vendor feed.

WHY ONE HELPER
--------------
Every generation-reports surface calls THIS module. Nobody re-derives the rule
inline (same doctrine as FleetStore.arrayStatus on the frontend): the workbook,
the billing unit, the client roster and the NEPOOL stats must agree by
construction, or the operator gets billed for a sheet that does not exist.

Nothing here mutates anything. This is a FILTER, not a data migration — vendor
twins keep their inverters, their telemetry and their place in the fleet-health
product. They are simply not part of the reports world.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import and_, exists, or_, select

from .models import Array, Client, Inverter, UtilityAccount


def not_vendor_only():
    """SQLAlchemy criterion: this ``Array`` is NOT a vendor-only twin.

    Correlates against the enclosing ``Array`` entity, so it drops straight
    into any existing ``select(Array)`` / ``select(func.count(Array.id))``
    without an extra round-trip:

        select(Array).where(Array.client_id == cid, not_vendor_only())

    Reads as: keep the array if it has a live utility account, OR if it has no
    inverters at all (the hand-added stub).
    """
    has_live_utility_account = exists(
        select(UtilityAccount.id).where(
            and_(
                UtilityAccount.array_id == Array.id,
                UtilityAccount.deleted_at.is_(None),
            )
        ).correlate(Array)
    )
    has_any_inverter = exists(
        select(Inverter.id).where(Inverter.array_id == Array.id).correlate(Array)
    )
    return or_(has_live_utility_account, ~has_any_inverter)


def is_vendor_only_array(db, array_id: int) -> bool:
    """True iff this single array is a vendor twin (see module docstring)."""
    if array_id is None:
        return False
    row = db.execute(
        select(Array.id).where(Array.id == array_id, not_vendor_only())
    ).first()
    if row is not None:
        return False
    # Not in the kept set — but make sure the array actually exists, so a bad
    # id reads as "unknown", not "vendor-only".
    return db.execute(select(Array.id).where(Array.id == array_id)).first() is not None


def utility_backed_array_ids(
    db,
    *,
    client_id: Optional[int] = None,
    tenant_id: Optional[str] = None,
    include_deleted: bool = False,
    include_excluded: bool = True,
) -> list[int]:
    """Ids of the reports-world arrays under a client and/or tenant.

    At least one of ``client_id`` / ``tenant_id`` should be given; with neither
    this returns every non-vendor-only array, which is almost never what a
    caller wants.
    """
    q = select(Array.id).where(not_vendor_only())
    if client_id is not None:
        q = q.where(Array.client_id == client_id)
    if tenant_id is not None:
        q = q.where(Array.tenant_id == tenant_id)
    if not include_deleted:
        q = q.where(Array.deleted_at.is_(None))
    if not include_excluded:
        q = q.where(Array.excluded.is_(False))
    return [r[0] for r in db.execute(q).all()]


def vendor_only_array_ids(
    db,
    *,
    client_id: Optional[int] = None,
    tenant_id: Optional[str] = None,
) -> list[int]:
    """The complement of :func:`utility_backed_array_ids` — for diagnostics,
    probes and tests. No production surface should need this."""
    q = select(Array.id).where(~not_vendor_only(), Array.deleted_at.is_(None))
    if client_id is not None:
        q = q.where(Array.client_id == client_id)
    if tenant_id is not None:
        q = q.where(Array.tenant_id == tenant_id)
    return [r[0] for r in db.execute(q).all()]


def filter_utility_backed(db, arrays: Iterable[Array]) -> list[Array]:
    """In-memory filter for callers that already hold ``Array`` objects.

    One extra query for the whole batch, then a set-membership test — so this
    stays cheap when a caller has loaded the rows for other reasons.
    """
    arrays = list(arrays)
    if not arrays:
        return []
    ids = [a.id for a in arrays]
    keep = {
        r[0]
        for r in db.execute(
            select(Array.id).where(Array.id.in_(ids), not_vendor_only())
        ).all()
    }
    return [a for a in arrays if a.id in keep]


__all__ = [
    "not_vendor_only",
    "is_vendor_only_array",
    "utility_backed_array_ids",
    "vendor_only_array_ids",
    "filter_utility_backed",
]
