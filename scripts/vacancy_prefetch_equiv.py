"""/vacancy tenant-wide prefetch: prod equivalence + the tie audit that justified
the bills-query tiebreaker. READ-ONLY — opens Sessions, never writes.

(One-off check for the market_vacancy N+1 fix, in the genre of
scripts/anna800_pricing_equiv.py; kept for re-runs after vacancy changes.)

Section 1 runs the pre-patch implementation (tests/_market_vacancy_pre_prefetch.py,
frozen from git 423a304d) and the shipped implementation over the SAME production
rows, for every tenant that has arrays, and diffs the payload leaf by leaf.
/vacancy prices real offtaker invoices, so "the tests pass" is not the bar — the
bar is that no leaf moved on real data.

Section 2 answers what the seeded suite cannot: prod DOES carry tied
(account_id, period_end) bills, `period_end DESC` alone is not a total order, and
`array_vacancy` then slices `bills[:12]` — so which tied bill survived the cut
was the planner's choice. The audit shows those ties are RE-CAPTURES of the same
month (same kWh, pulled twice) and that the higher id is essentially always the
later capture. That is what makes `ORDER BY period_end DESC, id DESC` the right
pin rather than an arbitrary one: on a tie, prefer the freshest capture.

Run (prod Postgres is on Railway's private network, so use the public proxy):

    export DATABASE_URL="$(railway variables --service Postgres --kv \
        | sed -n 's/^DATABASE_PUBLIC_URL=//p')"
    export SO_CONFIG_KEY="$(railway variables --kv | sed -n 's/^SO_CONFIG_KEY=//p')"
    .venv/bin/python scripts/vacancy_prefetch_equiv.py

SO_CONFIG_KEY is needed only by the PRE-patch side: its bare `select(Array)`
hydrates `Array.solaredge_api_key` (EncryptedStr) and decrypts it once per array.
The shipped code's column diet no longer touches that column at all.
"""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))

from sqlalchemy import func, select                            # noqa: E402

from api.db import SessionLocal                                # noqa: E402
from api.market_vacancy import tenant_vacancy                  # noqa: E402
from api.models import Array, Bill, UtilityAccount             # noqa: E402
from api.rate_schedule import excess_credit_rate_from_bill     # noqa: E402

import _market_vacancy_pre_prefetch as old                     # noqa: E402


def leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, path + "/%d" % i)
    else:
        yield path, obj


# ── 1. EQUIVALENCE, tenant by tenant ─────────────────────────────────────────

with SessionLocal() as db:
    tenant_ids = [t for (t,) in db.execute(
        select(Array.tenant_id).distinct().order_by(Array.tenant_id)).all()]

print("PROD EQUIVALENCE — %d tenants with arrays" % len(tenant_ids))
print("%-24s %7s %9s %9s %9s  %s"
      % ("tenant", "arrays", "leaves", "differ", "bytes", "verdict"))

total_leaves = total_diff = 0
bad = []
for tid in tenant_ids:
    with SessionLocal() as db:
        before = old.tenant_vacancy(db, tid)
    with SessionLocal() as db:
        after = tenant_vacancy(db, tid)

    a, b = dict(before), dict(after)
    # The only field that is a clock read rather than a measurement.
    a.pop("generated_at", None)
    b.pop("generated_at", None)
    la, lb = dict(leaves(a)), dict(leaves(b))
    keys = set(la) | set(lb)
    differ = sorted(k for k in keys if la.get(k, "<missing>") != lb.get(k, "<missing>"))
    sa, sb = json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True)
    total_leaves += len(keys)
    total_diff += len(differ)
    if sa != sb:
        bad.append((tid, differ, la, lb))
    print("%-24s %7d %9d %9d %9d  %s"
          % (tid[:24], before["totals"]["array_count"], len(keys), len(differ),
             len(sa), "IDENTICAL" if sa == sb else "DIFFERS"))

print("\nTOTAL: %d leaf fields compared, %d differ" % (total_leaves, total_diff))
for tid, differ, la, lb in bad:
    print("\n  %s — %d differing fields:" % (tid, len(differ)))
    for k in differ[:40]:
        print("    %-46s old=%-16r new=%r" % (k, la.get(k), lb.get(k)))

money = [k for _, differ, _, _ in bad for k in differ if not k.endswith("/credit_rate")]
print("\nMONEY FIELDS MOVED:", (money[:20] if money else "NONE"))


# ── 2. TIE AUDIT — why the bills query is pinned with `id DESC` ───────────────

print("\n" + "=" * 78)
print("TIE AUDIT — (account_id, period_end) groups with more than one excess bill")
with SessionLocal() as db:
    host_ids = {hid for (hid,) in db.execute(
        select(func.min(UtilityAccount.id))
        .where(UtilityAccount.array_id.isnot(None))
        .group_by(UtilityAccount.array_id)).all() if hid is not None}
    groups = db.execute(
        select(Bill.account_id, Bill.period_end)
        .where(Bill.period_end.isnot(None), Bill.kwh_sent_to_grid.isnot(None),
               Bill.kwh_sent_to_grid > 0)
        .group_by(Bill.account_id, Bill.period_end)
        .having(func.count(Bill.id) > 1)).all()
    keys = {(g.account_id, g.period_end) for g in groups}
    print("  host accounts (min-id per array): %d" % len(host_ids))
    print("  tied groups: %d · of those on a HOST account: %d"
          % (len(keys), sum(1 for k in keys if k[0] in host_ids)))

    rows = db.execute(
        select(Bill.account_id, Bill.period_end, Bill.id, Bill.pulled_at,
               Bill.kwh_sent_to_grid, Bill.raw_json)
        .where(Bill.account_id.in_({k[0] for k in keys} or {-1}),
               Bill.period_end.isnot(None), Bill.kwh_sent_to_grid.isnot(None),
               Bill.kwh_sent_to_grid > 0)).all()
    by_key: dict = {}
    for r in rows:
        if (r.account_id, r.period_end) in keys:
            by_key.setdefault((r.account_id, r.period_end), []).append(r)

    newer = older = flat = same_rate = same_kwh = 0
    for grp in by_key.values():
        hi = max(grp, key=lambda r: r.id)
        lo = min(grp, key=lambda r: r.id)
        if hi.pulled_at is None or lo.pulled_at is None or hi.pulled_at == lo.pulled_at:
            flat += 1
        elif hi.pulled_at > lo.pulled_at:
            newer += 1
        else:
            older += 1
        if excess_credit_rate_from_bill(hi.raw_json) == excess_credit_rate_from_bill(lo.raw_json):
            same_rate += 1
        if hi.kwh_sent_to_grid == lo.kwh_sent_to_grid:
            same_kwh += 1

    n = max(len(by_key), 1)
    print("  tied groups inspected: %d" % len(by_key))
    print("    highest id is the LATER capture:          %6d (%5.1f%%)" % (newer, 100.0 * newer / n))
    print("    highest id is the EARLIER capture:        %6d (%5.1f%%)" % (older, 100.0 * older / n))
    print("    same timestamp / unknown:                 %6d (%5.1f%%)" % (flat, 100.0 * flat / n))
    print("    same kWh (a re-capture, not a new month): %6d (%5.1f%%)"
          % (same_kwh, 100.0 * same_kwh / n))
    print("    tie is RATE-NEUTRAL either way:           %6d (%5.1f%%)"
          % (same_rate, 100.0 * same_rate / n))
