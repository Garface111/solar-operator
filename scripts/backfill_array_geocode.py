"""One-off: backfill Array.latitude/longitude for every array that has a
linked UtilityAccount with a real service_address but was never geocoded
(Array.latitude is None) — closing the "set location" gap for any array whose
weather-model was blocked only because nobody had triggered the lazy geocode
for it yet (Ford, 2026-07-01).

Verified this address format ALWAYS geocodes fine via Census, even with GMP's
"SOLAR ARRAY"/"SOLAR" suffixes appended to the street (e.g. "306 EDDY RD SOLAR
ARRAY" -> matched "306 EDDY RD, CHESTER, VT, 05143") — this isn't a data-
quality fix, purely a "run the geocode that was always going to succeed."

Only touches arrays with Array.latitude IS NULL (never overwrites a manual
override, a prior geocode, or a vendor-captured location) and a linked
UtilityAccount whose service_address resolves to a usable oneline string.
Safe to re-run — already-geocoded arrays are skipped every time.

SAFETY: default is a DRY RUN that prints exactly what it would change,
mutating nothing. Rate-limited (one geocode call per ~1.1s) to stay polite to
the free Census/Nominatim geocoders.
Run (dry):  python -m scripts.backfill_array_geocode
Run (live): railway ssh --service web "cd /app && python -m scripts.backfill_array_geocode --confirm-live"
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIRM_LIVE = "--confirm-live" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    i = sys.argv.index("--limit")
    if i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[i + 1])


def main() -> None:
    from sqlalchemy import select
    from api.db import SessionLocal
    from api.models import Array, UtilityAccount
    from api import forecasting

    do_it = CONFIRM_LIVE
    print(f"{'EXECUTING' if do_it else 'DRY RUN (no changes)'}"
          + (f", limit={LIMIT}" if LIMIT else "") + "\n")

    with SessionLocal() as db:
        rows = db.execute(
            select(Array, UtilityAccount)
            .join(UtilityAccount, UtilityAccount.array_id == Array.id)
            .where(
                Array.deleted_at.is_(None),
                Array.latitude.is_(None),
                UtilityAccount.service_address.isnot(None),
            )
        ).all()

    print(f"{len(rows)} candidate array(s) with a linked utility address and no location yet.\n")

    geocoded = skipped_no_address = skipped_failed = 0
    for arr, ua in rows:
        if LIMIT and geocoded >= LIMIT:
            break
        oneline = forecasting.address_to_oneline(ua.service_address)
        if not oneline:
            skipped_no_address += 1
            print(f"  SKIP  array={arr.id} {arr.name!r} tenant={arr.tenant_id} — "
                  f"service_address didn't reduce to a usable string.")
            continue
        geo = forecasting.geocode_oneline(oneline)
        if not geo:
            skipped_failed += 1
            print(f"  FAIL  array={arr.id} {arr.name!r} tenant={arr.tenant_id} — "
                  f"geocode_oneline({oneline!r}) returned None.")
            time.sleep(1.1)
            continue
        print(f"  GEOCODE array={arr.id} {arr.name!r} tenant={arr.tenant_id}: "
              f"{oneline!r} -> ({geo['lat']:.5f}, {geo['lng']:.5f}) via {geo['source']} "
              f"[{geo.get('matched')}]")
        if do_it:
            with SessionLocal() as db2:
                from datetime import datetime
                a2 = db2.get(Array, arr.id)
                # Re-check under a fresh session — never clobber a location that
                # landed (e.g. via a vendor capture) since we read the candidate list.
                if a2 is not None and a2.latitude is None:
                    a2.latitude, a2.longitude = geo["lat"], geo["lng"]
                    a2.geocode_source = geo["source"][:24]
                    a2.geocoded_address = (geo.get("matched") or oneline)[:500]
                    a2.geocoded_at = datetime.utcnow()
                    db2.commit()
        geocoded += 1
        time.sleep(1.1)   # stay polite to the free keyless geocoders

    print(f"\n{geocoded} geocoded, {skipped_no_address} skipped (no usable address), "
          f"{skipped_failed} failed to resolve.")
    if not do_it:
        print("(dry run — re-run with --confirm-live to apply)")


if __name__ == "__main__":
    main()
