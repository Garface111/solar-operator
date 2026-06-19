"""One-off: deep multi-year SolarEdge daily-energy backfill for a tenant.

The nightly inverter pull only reaches ~90 days back, so arrays connected via
SolarEdge show just the current year in Trends. SolarEdge's /site/{id}/energy
?timeUnit=DAY endpoint serves up to ~1 year per request, so we chunk year-by-
year from START_YEAR..today and upsert every day with data into DailyGeneration
(source='solaredge'). This is REAL metered history we already hold credentials
for — not synthetic.

Idempotent: upsert by (array_id, day); re-running refreshes values. Reads
SolarEdge creds from each array's InverterConnection.config (api_key+site_id),
falling back to legacy Array.solaredge_* columns.

Usage (on prod image):
  railway ssh "python -m scripts.backfill_solaredge_history --tenant <tid> --since 2017"
"""
from __future__ import annotations

import argparse
from datetime import date
from sqlalchemy import select

from api.db import SessionLocal
from api.models import Array, DailyGeneration, InverterConnection, now
from api.adapters.solaredge import fetch_daily_energy, SolarEdgeError


def _se_creds(db, arr: Array):
    """(api_key, site_id) for an array's SolarEdge feed, or None."""
    c = db.execute(
        select(InverterConnection).where(
            InverterConnection.array_id == arr.id,
            InverterConnection.vendor == "solaredge",
        )
    ).scalars().first()
    if c and (c.config or {}).get("api_key") and (c.config or {}).get("site_id"):
        return c.config["api_key"], int(c.config["site_id"])
    if arr.solaredge_api_key and arr.solaredge_site_id:
        return arr.solaredge_api_key, int(arr.solaredge_site_id)
    return None


def backfill_array(db, arr: Array, since_year: int) -> dict:
    creds = _se_creds(db, arr)
    if not creds:
        return {"array_id": arr.id, "skipped": "no solaredge creds"}
    api_key, site_id = creds
    today = date.today()
    # pull existing days once so we update-in-place without a query per day
    existing = {
        r.day: r for r in db.execute(
            select(DailyGeneration).where(DailyGeneration.array_id == arr.id)
        ).scalars().all()
    }
    ins = upd = 0
    first_day = None
    for yr in range(since_year, today.year + 1):
        start = date(yr, 1, 1)
        end = date(yr, 12, 31) if yr < today.year else today
        try:
            entries = fetch_daily_energy(api_key, site_id, start, end)
        except SolarEdgeError as ex:
            print(f"    {yr}: ERR {ex}")
            continue
        for e in entries:
            d, kwh = e["day"], e["kwh"]
            if not kwh:
                continue
            if first_day is None or d < first_day:
                first_day = d
            row = existing.get(d)
            if row is not None:
                # never clobber a non-solaredge real source with this pull
                if row.source not in ("solaredge", "bill_prorate", None):
                    continue
                row.kwh = kwh
                row.source = "solaredge"
                row.uploaded_at = now()
                upd += 1
            else:
                ng = DailyGeneration(tenant_id=arr.tenant_id, array_id=arr.id,
                                     day=d, kwh=kwh, source="solaredge")
                db.add(ng); existing[d] = ng; ins += 1
    db.commit()
    return {"array_id": arr.id, "name": arr.name, "site_id": site_id,
            "inserted": ins, "updated": upd, "first_day": str(first_day)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--since", type=int, default=2017)
    args = ap.parse_args()
    with SessionLocal() as db:
        arrays = db.execute(
            select(Array).where(Array.tenant_id == args.tenant,
                                Array.deleted_at.is_(None)).order_by(Array.id)
        ).scalars().all()
        print(f"tenant {args.tenant}: {len(arrays)} arrays, since {args.since}")
        tot_ins = tot_upd = 0
        for arr in arrays:
            r = backfill_array(db, arr, args.since)
            if r.get("skipped"):
                continue
            print(f"  array {r['array_id']} {r['name'][:24]:24s} site={r['site_id']} "
                  f"+{r['inserted']} ~{r['updated']} from {r['first_day']}")
            tot_ins += r["inserted"]; tot_upd += r["updated"]
        print(f"DONE inserted={tot_ins} updated={tot_upd}")


if __name__ == "__main__":
    main()
