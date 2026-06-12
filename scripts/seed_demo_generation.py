"""Seed CUSTOMER ZERO demo generation — realistic VT solar export for WEC + VEC.

WHY THIS EXISTS
  Both WEC (washingtonelectric.smarthub.coop) and VEC (vermontelectric.smarthub.coop)
  are NISC SmartHub deployments and api/adapters/smarthub.py is the proven shared
  adapter — scripts/provision_customer_zero.py --live already authed to WEC acct
  #982501 and pulled 91 real days. But the accounts we can reach are
  consumption/credit meters: every RETURN-channel day comes back 0.0 kWh, so the
  customer-zero demo renders no solar generation.

  This script seeds believable generation through the SAME persistence path the
  real adapter uses — jobs/smarthub_pull.pull_daily_generation_for_account
  upserts DailyGeneration(tenant_id, array_id, day, kwh, source="smarthub") keyed
  by (array_id, day). We write identical rows, so the existing report writers
  (which prefer DailyGeneration for any month that has rows) read it with zero
  special-casing.

WHAT IT DOES (idempotent — safe to re-run, values are deterministic):
  1. Looks up tenant ten_ocicbb_customer_zero and the WEC array
     ("WEC – Evans (Maple Corner)").
  2. Creates a VEC array + UtilityAccount (provider=vec) mirroring the WEC one
     if missing (VEC has no live login yet — placeholder account number).
  3. Seeds daily kWh for the trailing 12 full months + current month-to-date,
     normalized so each month sums to a realistic Vermont residential export
     curve (~7 kW array: Dec/Jan ~250 kWh/mo, Jun/Jul ~900 kWh/mo), with
     deterministic weather-like daily variation. VEC is scaled to ~6.2 kW so
     the two arrays don't look cloned.
  4. Reads everything back and prints a month-by-month kWh table per array.

  Existing rows (including the 91 real zero-kWh WEC pulls) are UPDATED in
  place, exactly as a re-run of the real pull would update them.

DEMO DATA ONLY — touches only customer-zero tenant rows. Does not push,
deploy, or contact any utility.

Run:
    python3 scripts/seed_demo_generation.py
"""
from __future__ import annotations

import calendar
import hashlib
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from api.db import SessionLocal, init_db, DB_URL
from api.models import (
    Tenant, Client, Array, UtilityAccount, DailyGeneration, now,
)

TENANT_ID = "ten_ocicbb_customer_zero"
WEC_ARRAY_NAME = "WEC – Evans (Maple Corner)"
VEC_ARRAY_NAME = "VEC – Customer Zero (Demo)"
VEC_PLACEHOLDER = "VEC-PENDING-DISCOVERY"

# Monthly export targets (kWh) for a ~7 kW residential array in central VT.
# Net export through a net meter: low winter (snow cover, short days, more of
# the generation consumed on-site), peak early summer.
MONTHLY_BASE = {
    1: 255.0,   # Jan — snow, short days
    2: 370.0,
    3: 545.0,   # melt-off, cold-clear days
    4: 680.0,
    5: 830.0,
    6: 905.0,   # peak
    7: 885.0,
    8: 795.0,
    9: 640.0,
    10: 455.0,
    11: 290.0,
    12: 245.0,  # Dec — lowest
}

# Per-array scale so WEC and VEC are siblings, not clones (~7 kW vs ~6.2 kW),
# plus a per-array year flavor wiggle applied per-month deterministically.
ARRAY_PROFILES = {
    WEC_ARRAY_NAME: {"scale": 1.00, "salt": "wec-evans"},
    VEC_ARRAY_NAME: {"scale": 0.885, "salt": "vec-czero"},
}


def _unit_noise(salt: str, *parts: object) -> float:
    """Deterministic pseudo-random float in [0, 1) from salt + parts."""
    h = hashlib.sha256(("|".join([salt, *map(str, parts)])).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def month_target_kwh(salt: str, year: int, month: int, scale: float) -> float:
    """Monthly export target with ±7% deterministic weather wiggle."""
    base = MONTHLY_BASE[month] * scale
    wiggle = 1.0 + (_unit_noise(salt, "month", year, month) - 0.5) * 0.14
    return base * wiggle


def daily_series(salt: str, year: int, month: int, target: float,
                 last_day: int | None = None) -> dict[date, float]:
    """Split a monthly target across days with weather-like variation.

    Each day gets weight 0.25..1.75 (sunny vs overcast/snow), then the month is
    normalized to sum exactly to target. Deterministic per (salt, day).
    If last_day is given (current partial month) only days 1..last_day are
    emitted and the target is prorated by calendar share.
    """
    ndays = calendar.monthrange(year, month)[1]
    upto = min(last_day or ndays, ndays)
    if upto <= 0:
        return {}
    weights = {
        d: 0.25 + 1.5 * _unit_noise(salt, "day", year, month, d)
        for d in range(1, upto + 1)
    }
    # Prorate target for a partial month by day-count share.
    eff_target = target * (upto / ndays)
    total_w = sum(weights.values())
    return {
        date(year, month, d): round(eff_target * w / total_w, 4)
        for d, w in weights.items()
    }


def months_window(today: date, n_full: int = 12) -> list[tuple[int, int]]:
    """Trailing n_full complete months, oldest first, then current month."""
    first_of_this = today.replace(day=1)
    months: list[tuple[int, int]] = []
    y, m = first_of_this.year, first_of_this.month
    for _ in range(n_full):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        months.append((y, m))
    months.reverse()
    months.append((first_of_this.year, first_of_this.month))
    return months


def get_or_create_vec_array(db, tenant: Tenant, wec: Array) -> Array:
    a = db.execute(
        select(Array).where(
            Array.tenant_id == tenant.id, Array.name == VEC_ARRAY_NAME,
        )
    ).scalar_one_or_none()
    if a:
        if a.deleted_at is not None:
            a.deleted_at = None
            print(f"  VEC array resurrected: id={a.id}")
        else:
            print(f"  VEC array exists: id={a.id}")
    else:
        a = Array(
            tenant_id=tenant.id,
            client_id=wec.client_id,           # same self-client as WEC
            name=VEC_ARRAY_NAME,
            bill_offset_months=1,
            fuel_type="solar",
            notes=("Customer-zero dogfood, mirrors the WEC array. DEMO: "
                   "generation rows seeded by scripts/seed_demo_generation.py "
                   "(VEC login not yet staged)."),
        )
        db.add(a)
        db.flush()
        print(f"  VEC array created: id={a.id}")

    acc = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == tenant.id,
            UtilityAccount.array_id == a.id,
            UtilityAccount.provider == "vec",
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if acc:
        print(f"    VEC account exists: #{acc.account_number}")
    else:
        acc = UtilityAccount(
            tenant_id=tenant.id,
            array_id=a.id,
            provider="vec",
            account_number=VEC_PLACEHOLDER,
            nickname=a.name,
            extra={"customer_zero": True, "demo_seeded": True},
        )
        db.add(acc)
        db.flush()
        print(f"    VEC account created: #{acc.account_number}")
    return a


def seed_array(db, tenant_id: str, arr: Array, today: date) -> tuple[int, int]:
    """Upsert seeded daily rows for one array. Returns (inserted, updated).

    Mirrors jobs/smarthub_pull.pull_daily_generation_for_account upsert
    semantics exactly: keyed (array_id, day), source='smarthub',
    uploaded_at=now() on update.
    """
    prof = ARRAY_PROFILES[arr.name]
    rows: dict[date, float] = {}
    for (y, m) in months_window(today):
        target = month_target_kwh(prof["salt"], y, m, prof["scale"])
        partial = today.day - 1 if (y, m) == (today.year, today.month) else None
        if partial == 0:
            continue  # first of month, nothing to seed yet for current month
        rows.update(daily_series(prof["salt"], y, m, target, last_day=partial))

    days = list(rows.keys())
    existing = {
        r.day: r
        for r in db.execute(
            select(DailyGeneration).where(
                DailyGeneration.array_id == arr.id,
                DailyGeneration.day.in_(days),
            )
        ).scalars().all()
    }

    inserted = updated = 0
    for d, kwh in rows.items():
        if d in existing:
            row = existing[d]
            row.kwh = kwh
            row.source = "smarthub"
            row.uploaded_at = now()
            updated += 1
        else:
            db.add(DailyGeneration(
                tenant_id=tenant_id,
                array_id=arr.id,
                day=d,
                kwh=kwh,
                source="smarthub",
            ))
            inserted += 1
    return inserted, updated


def readback_table(db, arrays: list[Array]) -> None:
    print("\nREADBACK (monthly kWh summed from daily_generation):")
    header = f"  {'month':<9}" + "".join(f"{a.name[:28]:>30}" for a in arrays)
    print(header)
    months_seen: dict[str, dict[int, float]] = {}
    counts: dict[int, int] = {}
    for a in arrays:
        rows = db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == a.id)
        ).scalars().all()
        counts[a.id] = len(rows)
        for r in rows:
            key = f"{r.day.year}-{r.day.month:02d}"
            months_seen.setdefault(key, {})[a.id] = (
                months_seen.get(key, {}).get(a.id, 0.0) + r.kwh
            )
    for key in sorted(months_seen):
        line = f"  {key:<9}"
        for a in arrays:
            v = months_seen[key].get(a.id)
            line += f"{(f'{v:,.1f}' if v is not None else '—'):>30}"
        print(line)
    total_line = f"  {'TOTAL':<9}"
    nrow_line = f"  {'rows':<9}"
    for a in arrays:
        tot = sum(m.get(a.id, 0.0) for m in months_seen.values())
        total_line += f"{tot:>30,.1f}"
        nrow_line += f"{counts[a.id]:>30}"
    print(total_line)
    print(nrow_line)


def main() -> None:
    print(f"DB: {DB_URL}")
    init_db()
    today = date.today()

    with SessionLocal() as db:
        tenant = db.get(Tenant, TENANT_ID)
        if tenant is None:
            raise SystemExit(
                f"tenant {TENANT_ID} not found — run "
                "scripts/provision_customer_zero.py first")
        print(f"  tenant: {tenant.id} ({tenant.name})")

        wec = db.execute(
            select(Array).where(
                Array.tenant_id == TENANT_ID,
                Array.name == WEC_ARRAY_NAME,
                Array.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if wec is None:
            raise SystemExit(f"WEC array {WEC_ARRAY_NAME!r} not found — run "
                             "scripts/provision_customer_zero.py first")
        print(f"  WEC array: id={wec.id}")

        vec = get_or_create_vec_array(db, tenant, wec)

        for arr in (wec, vec):
            ins, upd = seed_array(db, TENANT_ID, arr, today)
            print(f"  seeded {arr.name!r}: inserted={ins} updated={upd}")

        db.commit()
        readback_table(db, [wec, vec])


if __name__ == "__main__":
    main()
