"""Consolidate duplicate UtilitySession rows — DRY-RUN BY DEFAULT.

WHY THIS EXISTS
---------------
GMP operator logins manage many utility customers under one login, so every
capture's accounts carry many distinct customer_numbers and
`session_customer_number()` returns None. Before the upsert fix
(api/app.py, 2026-06-21) every capture INSERTed a new unkeyed row, so prod
accumulated 2-6 UtilitySession rows per (tenant, provider) — all with
customer_number NULL. That made "which session is authoritative" ambiguous and
(before the scheduler tenant-level de-dup) multiplied reauth emails.

The capture upsert STOPS new duplicates. This script cleans up the EXISTING
dupes already in prod, soft-merging each (tenant, provider, customer_number)
group down to ONE authoritative row.

SAFETY DOCTRINE (Bruce's tenants are LIVE prod data)
----------------------------------------------------
  * DRY-RUN by default. It only writes when CONSOLIDATE_EXECUTE=1 is set.
  * NEVER hard-deletes. Losers are SOFT-retired: refresh_token is nulled (so the
    scheduler stops trying to refresh them and they drop out of the refresh
    query) and an audit breadcrumb is stamped into raw_payload. The row, its
    api_token, expires_at, captured_at and customer_number all stay intact.
  * Winner per group = newest captured_at (the row `latest_session()` actually
    selects), tie-broken by lowest refresh_failures then highest id. So
    consolidation never changes which token is used for pulls.
  * Reversible: clear raw_payload['_superseded'] to undo (the original
    refresh_token is preserved under raw_payload['_superseded']['refresh_token']).

USAGE (via the railway ssh base64 transport, from /app):
    # dry-run (read-only — counts only):
    python -m scripts.consolidate_gmp_sessions
    # execute (ONLY after Ford sign-off):
    CONSOLIDATE_EXECUTE=1 python -m scripts.consolidate_gmp_sessions

Env knobs:
    CONSOLIDATE_PROVIDER=gmp   # restrict to one provider (default: gmp)
                               # set to 'all' to sweep every provider
    CONSOLIDATE_EXECUTE=1      # actually write (default: dry-run)

AFTER consolidation has run clean (every group down to 1 live-refresh row), the
belt-and-suspenders DB guard can be added — see the note at the bottom of this
file. Do NOT add it to api/migrate.py until consolidation is done: migrate.py
runs on every deploy (railway.toml startCommand) and a UNIQUE constraint over
still-duplicated rows would brick the boot.
"""
from __future__ import annotations

import os
from collections import defaultdict

from sqlalchemy import select

from api.db import SessionLocal
from api.models import UtilitySession, now


def _winner(group: list[UtilitySession]) -> UtilitySession:
    # newest captured_at, then fewest failures, then highest id — the row
    # latest_session() would pick, so selection is unchanged by consolidation.
    return sorted(
        group,
        key=lambda s: (s.captured_at, -(s.refresh_failures or 0), s.id),
        reverse=True,
    )[0]


def main() -> None:
    execute = os.environ.get("CONSOLIDATE_EXECUTE") == "1"
    provider = os.environ.get("CONSOLIDATE_PROVIDER", "gmp")
    mode = "EXECUTE (writing)" if execute else "DRY-RUN (read-only)"
    scope = "ALL providers" if provider == "all" else f"provider={provider}"
    print(f"=== consolidate_gmp_sessions :: {mode} :: {scope} ===")

    with SessionLocal() as db:
        q = select(UtilitySession)
        if provider != "all":
            q = q.where(UtilitySession.provider == provider)
        rows = db.execute(q).scalars().all()

        groups: dict[tuple, list[UtilitySession]] = defaultdict(list)
        for s in rows:
            groups[(s.tenant_id, s.provider, s.customer_number)].append(s)

        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        total_losers = sum(len(v) - 1 for v in dup_groups.values())
        print(f"sessions scanned: {len(rows)}  | groups: {len(groups)}  | "
              f"dup groups: {len(dup_groups)}  | rows to retire: {total_losers}")

        retired = 0
        for (tenant_id, prov, cust), group in sorted(
            dup_groups.items(), key=lambda kv: -len(kv[1])
        ):
            win = _winner(group)
            losers = [s for s in group if s.id != win.id]
            print(f"\n  tenant={tenant_id} provider={prov} cust={cust!r} "
                  f"({len(group)} rows)")
            print(f"    KEEP  id={win.id} captured={win.captured_at} "
                  f"fails={win.refresh_failures} expires={win.expires_at}")
            for s in losers:
                already = bool((s.raw_payload or {}).get("_superseded"))
                tag = " [already retired]" if already else ""
                print(f"    retire id={s.id} captured={s.captured_at} "
                      f"fails={s.refresh_failures} rt={'yes' if s.refresh_token else 'NO'}{tag}")
                if execute and not already:
                    payload = dict(s.raw_payload or {})
                    payload["_superseded"] = {
                        "by": win.id,
                        "at": now().isoformat(),
                        "refresh_token": s.refresh_token,  # preserved for undo
                        "reason": "dup-session consolidation 2026-06",
                    }
                    s.raw_payload = payload
                    s.refresh_token = None  # soft-retire: drops out of refresh query
                    retired += 1

        if execute:
            db.commit()
            print(f"\n=== EXECUTED: {retired} row(s) soft-retired (refresh_token "
                  f"nulled, audit stamped). No rows deleted. ===")
        else:
            print(f"\n=== DRY-RUN: would soft-retire {total_losers} row(s). "
                  f"Set CONSOLIDATE_EXECUTE=1 to apply (after sign-off). ===")


if __name__ == "__main__":
    main()


# ── BELT-AND-SUSPENDERS DB GUARD (apply only AFTER a clean consolidation) ─────
#
# The app-level upsert (api/app.py) prevents the common reconnect-accumulation
# case, but two truly-concurrent capture POSTs in the same instant could still
# race past the SELECT and both INSERT (prod showed same-second bursts, e.g.
# ids 127-130 within 0.4s). A DB unique constraint closes that race.
#
# Postgres 18 (prod is 18.4) supports NULLS NOT DISTINCT, which is REQUIRED here
# because the GMP bucket key is customer_number = NULL and a plain UNIQUE treats
# every NULL as distinct (so it would NOT prevent duplicate NULL rows):
#
#   ALTER TABLE utility_sessions
#     ADD CONSTRAINT uq_utility_session_tenant_provider_cust
#     UNIQUE NULLS NOT DISTINCT (tenant_id, provider, customer_number);
#
# Preconditions before running it:
#   1. The capture upsert (api/app.py) is deployed.       [done 2026-06-21]
#   2. consolidate_gmp_sessions.py has run with EXECUTE and every group is down
#      to exactly one live-refresh row (re-run this script in dry-run; it must
#      report "dup groups: 0").  ← gate the ALTER on this.
#   3. Apply the ALTER manually (railway ssh) and verify, THEN — and only then —
#      add the matching idempotent ALTER to api/migrate.py so fresh DBs get it.
#      Do not add it to migrate.py earlier: migrate.py runs on every deploy and
#      the ALTER would fail (and brick the boot) while duplicates still exist.
