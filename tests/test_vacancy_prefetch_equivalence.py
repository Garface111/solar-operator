"""The tenant-wide vacancy prefetch must not move a cent.

/vacancy prices real offtaker invoices, so a latency patch here earns exactly
one kind of proof: run the OLD arithmetic and the NEW arithmetic over the same
seeded fleet and diff the JSON byte for byte. The old implementation is the
frozen snapshot in `_market_vacancy_pre_prefetch.py` (generated from git
423a304d — see its header), NOT a paraphrase and NOT git HEAD.

Four proofs:
  1. EQUIVALENCE — `tenant_vacancy` returns byte-identical JSON, and every
     single-array `array_vacancy` call does too, over a fleet built to walk every
     branch: multi-account arrays, arrays with no account, accounts with no
     bills, disabled subscriptions, allocation_pct fallback, missing raw_json,
     bills that state no credit rate (the `_bill_credit_rate` fallback ladder),
     excluded arrays, and a >window bill history.
  2. THE PREFETCH IS INERT — `array_vacancy` with and without a prefetch agree,
     and a prefetch built for OTHER arrays is ignored rather than misapplied.
  3. N+1 IS DEAD — total SQL statements are CONSTANT as the fleet grows, counted
     at the cursor (so `Session.get`, which never goes through `Session.execute`,
     is counted too).
  4. NO LEAKAGE — a second tenant's arrays never enter the first tenant's answer.

Uses a unique provider string so this file's bills can't drift the cross-tenant
fleet median other tests assert on (same isolation trick as
tests/test_fleet_credit_memo_equivalence.py).
"""
import json
import secrets
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from api.db import SessionLocal, engine
from api.models import (Tenant, Array, UtilityAccount, Bill,
                        BillingReportSubscription, Client, now as _now)
from api.market_vacancy import (array_vacancy, tenant_vacancy,
                                _prefetch_vacancy, VacancyPrefetch)

import _market_vacancy_pre_prefetch as old

PROV = "zzz_vac_prefetch_" + secrets.token_hex(3)


# ── SQL counting at the cursor ────────────────────────────────────────────────

class count_sql:
    """Count every statement that reaches the DBAPI cursor.

    Deliberately NOT a `Session.execute` wrapper: `Session.get` emits SQL through
    the session's internal loader, never through `Session.execute`, and three of
    the five per-array queries this patch removes were `db.get` calls. Counting
    at `before_cursor_execute` is the only count that sees all of them."""

    def __enter__(self):
        self.statements = []

        @event.listens_for(engine, "before_cursor_execute")
        def _on(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

        self._fn = _on
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._fn)
        return False

    @property
    def n(self):
        return len(self.statements)


# ── seeding ───────────────────────────────────────────────────────────────────

def _excess_json(*, shared_kwh=0.0, credited_kwh=0.0, credited_usd=0.0):
    """GMP-shaped raw_json: a $0 EXCESS line (shared out to members) and/or a
    negative-$ credited residual line (retained + cashed by the host)."""
    items = []
    if shared_kwh:
        items.append({"unitOfMeasure": "KWH", "unitCode": "EXCESS",
                      "unitCount": shared_kwh, "dollarAmount": 0.0})
    if credited_kwh:
        items.append({"unitOfMeasure": "KWH", "unitCode": "EXCESS",
                      "unitCount": credited_kwh, "dollarAmount": -abs(credited_usd)})
    return {"billSegments": [{"segmentLineItems": items}]}


def _mk_array(db, tid, name, *, excluded=False, first_connect=datetime(2016, 5, 1)):
    a = Array(tenant_id=tid, name=name, region="VT",
              first_connect_date=first_connect, excluded=excluded)
    db.add(a)
    db.flush()
    return a


def _mk_account(db, tid, array_id, tag):
    acct = UtilityAccount(tenant_id=tid, provider=PROV, array_id=array_id,
                          account_number=f"{tag}-{secrets.token_hex(3)}",
                          nickname=tag)
    db.add(acct)
    db.flush()
    return acct


def _mk_bills(db, tid, acct_id, *, n, spacing_days, pool, raw_maker,
              solar_credit_usd, start_days_ago=3):
    """`n` host bills walking backwards from `start_days_ago`."""
    base = _now() - timedelta(days=start_days_ago)
    for i in range(n):
        pe = base - timedelta(days=spacing_days * i)
        db.add(Bill(tenant_id=tid, account_id=acct_id,
                    period_start=pe - timedelta(days=spacing_days - 1),
                    period_end=pe,
                    kwh_generated=int(pool * 1.02),
                    kwh_sent_to_grid=pool + i,      # distinct pools per month
                    solar_credit_usd=solar_credit_usd,
                    raw_json=raw_maker(i) if raw_maker else None,
                    is_net_metered=True))


def _mk_subs(db, tid, array_id, shares, *, enabled=True, use_allocation_pct=False):
    for i, sh in enumerate(shares):
        c = Client(tenant_id=tid, name=f"off{i}-{secrets.token_hex(2)}", active=True)
        db.add(c)
        db.flush()
        db.add(BillingReportSubscription(
            tenant_id=tid, client_id=c.id, customer_name=f"off{i}",
            array_id=array_id,
            allocation_pct=(sh if use_allocation_pct else 1.0),
            array_share_pct=(None if use_allocation_pct else sh),
            utility_account_id=None, billing_model="percent_of_array",
            cadence="monthly", enabled=enabled))
    db.flush()


def _seed_fleet(tid, *, n_filler=0):
    """One tenant whose arrays walk every branch of array_vacancy.

    `n_filler` adds plain arrays of the same shape — used only by the SQL-count
    test, which needs two fleets of different SIZE but identical shape."""
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name=tid,
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()

        # 1. Perpetual banker — retained on host, no $0 shared line, BANKED.
        a = _mk_array(db, tid, "Banker")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=12, spacing_days=30, pool=56000.0,
                  raw_maker=lambda i: _excess_json(credited_kwh=9, credited_usd=1.66),
                  solar_credit_usd=None)
        _mk_subs(db, tid, a.id, [0.02])

        # 2. Fully allocated — whole pool on the $0 shared line.
        a = _mk_array(db, tid, "Allocated")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=12, spacing_days=30, pool=30000.0,
                  raw_maker=lambda i: _excess_json(shared_kwh=29900, credited_kwh=100,
                                                   credited_usd=18.4),
                  solar_credit_usd=41.2)
        _mk_subs(db, tid, a.id, [0.50, 0.497])

        # 3. THREE utility accounts: host is MIN(id); the other two carry bills
        #    that must NOT be counted (the old ORDER BY id LIMIT 1 ignored them,
        #    and the new MIN(id) GROUP BY must ignore them identically).
        a = _mk_array(db, tid, "MultiAccount")
        h = _mk_account(db, tid, a.id, "host")
        sib1 = _mk_account(db, tid, a.id, "sib1")
        sib2 = _mk_account(db, tid, a.id, "sib2")
        _mk_bills(db, tid, h.id, n=9, spacing_days=30, pool=18000.0,
                  raw_maker=lambda i: _excess_json(shared_kwh=9000, credited_kwh=40,
                                                   credited_usd=7.2),
                  solar_credit_usd=7.2)
        for sib in (sib1, sib2):
            _mk_bills(db, tid, sib.id, n=9, spacing_days=30, pool=99999.0,
                      raw_maker=lambda i: _excess_json(credited_kwh=500, credited_usd=95.0),
                      solar_credit_usd=None)
        _mk_subs(db, tid, a.id, [0.25])

        # 4. MORE bills than the window (18 > 12) → exercises the [:12] slice.
        a = _mk_array(db, tid, "LongHistory")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=18, spacing_days=19, pool=7000.0,
                  raw_maker=lambda i: _excess_json(shared_kwh=3000 + i,
                                                   credited_kwh=60 + i,
                                                   credited_usd=11.0 + i * 0.13),
                  solar_credit_usd=None)
        _mk_subs(db, tid, a.id, [0.40])

        # 5. No utility account at all → host_id None, registry still speaks.
        a = _mk_array(db, tid, "NoAccount")
        _mk_subs(db, tid, a.id, [0.33])

        # 6. Account but no bills → registry-only fallback.
        a = _mk_array(db, tid, "NoBills")
        _mk_account(db, tid, a.id, "host")
        _mk_subs(db, tid, a.id, [0.80])

        # 7. Bills that STATE NO CREDIT RATE (no credited line) on an account that
        #    has NEVER cashed → walks the whole _bill_credit_rate ladder:
        #    bill rate → _account_credit_rate → _fleet_credit_rate → DEFAULT.
        #    This is the branch the host_account/array threading touches.
        a = _mk_array(db, tid, "NoStatedRate", first_connect=datetime(2004, 1, 1))
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=6, spacing_days=30, pool=12000.0,
                  raw_maker=lambda i: _excess_json(shared_kwh=5000),
                  solar_credit_usd=None)
        _mk_subs(db, tid, a.id, [0.10])

        # 8. raw_json entirely absent → has_lines False → retained = whole pool.
        a = _mk_array(db, tid, "NoRawJson")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=5, spacing_days=30, pool=4000.0,
                  raw_maker=None, solar_credit_usd=None)
        _mk_subs(db, tid, a.id, [0.05])

        # 9. Only DISABLED subscriptions → registry must stay silent (None).
        a = _mk_array(db, tid, "DisabledSubsOnly")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=4, spacing_days=30, pool=2500.0,
                  raw_maker=lambda i: _excess_json(credited_kwh=12, credited_usd=2.2),
                  solar_credit_usd=2.2)
        _mk_subs(db, tid, a.id, [0.60, 0.10], enabled=False)

        # 10. MANY subs with float-trap shares, plus one disabled that must be
        #     excluded. 0.1+0.2+0.3+0.15+0.05 is order-sensitive in binary float —
        #     if the new ORDER BY changed the summation order, the 4-dp rounding
        #     of registry_allocated_frac is where it would show.
        a = _mk_array(db, tid, "ManySubs")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=7, spacing_days=30, pool=9100.0,
                  raw_maker=lambda i: _excess_json(shared_kwh=4000, credited_kwh=33,
                                                   credited_usd=6.1),
                  solar_credit_usd=6.1)
        _mk_subs(db, tid, a.id, [0.1, 0.2, 0.3, 0.15, 0.05])
        _mk_subs(db, tid, a.id, [0.99], enabled=False)

        # 11. array_share_pct NULL → allocation_pct is the share.
        a = _mk_array(db, tid, "AllocPctFallback")
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=3, spacing_days=30, pool=1500.0,
                  raw_maker=lambda i: _excess_json(shared_kwh=700, credited_kwh=20,
                                                   credited_usd=3.9),
                  solar_credit_usd=3.9)
        _mk_subs(db, tid, a.id, [0.45, 0.05], use_allocation_pct=True)

        # 12. EXCLUDED → skipped by tenant_vacancy entirely.
        a = _mk_array(db, tid, "Excluded", excluded=True)
        h = _mk_account(db, tid, a.id, "host")
        _mk_bills(db, tid, h.id, n=6, spacing_days=30, pool=77000.0,
                  raw_maker=lambda i: _excess_json(credited_kwh=88, credited_usd=16.2),
                  solar_credit_usd=None)
        _mk_subs(db, tid, a.id, [0.01])

        # 13. Telemetry-only: account, no bills, no subs → dropped from the rollup.
        a = _mk_array(db, tid, "BareTelemetry")
        _mk_account(db, tid, a.id, "host")

        for k in range(n_filler):
            a = _mk_array(db, tid, f"Filler{k}")
            h = _mk_account(db, tid, a.id, "host")
            _mk_bills(db, tid, h.id, n=6, spacing_days=30, pool=5000.0 + k,
                      raw_maker=lambda i: _excess_json(shared_kwh=2000, credited_kwh=25,
                                                       credited_usd=4.6),
                      solar_credit_usd=4.6)
            _mk_subs(db, tid, a.id, [0.30])

        db.commit()
    return tid


TID = "ten_vacpre_" + secrets.token_hex(3)
OTHER_TID = "ten_vacother_" + secrets.token_hex(3)


@pytest.fixture(scope="module", autouse=True)
def seeded():
    _seed_fleet(TID)
    # A second tenant with the SAME array names and a much bigger money leak —
    # anything that leaked across the boundary would be loud.
    _seed_fleet(OTHER_TID)
    yield


def _comparable(payload):
    """The payload minus the one field that is a clock read, not a measurement."""
    out = json.loads(json.dumps(payload))
    assert out.pop("generated_at", None), "generated_at must be present"
    return out


def _leaf_count(obj):
    if isinstance(obj, dict):
        return sum(_leaf_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_leaf_count(v) for v in obj)
    return 1


# ── 1. EQUIVALENCE ────────────────────────────────────────────────────────────

def test_tenant_vacancy_json_is_byte_identical_to_the_pre_prefetch_code():
    with SessionLocal() as db_old:
        before = old.tenant_vacancy(db_old, TID)
    with SessionLocal() as db_new:
        after = tenant_vacancy(db_new, TID)

    a, b = _comparable(before), _comparable(after)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    # Guard against a vacuous pass: the fleet must actually have measured money.
    assert a["totals"]["array_count"] >= 10, a["totals"]
    assert a["totals"]["vacancy_usd"] > 0
    assert _leaf_count(a) > 150, _leaf_count(a)
    # Every branch we seeded is represented.
    names = {r["array_name"] for r in a["arrays"]}
    assert {"Banker", "Allocated", "MultiAccount", "LongHistory", "NoAccount",
            "NoBills", "NoStatedRate", "NoRawJson", "DisabledSubsOnly",
            "ManySubs", "AllocPctFallback"} <= names, names
    assert "Excluded" not in names and "BareTelemetry" not in names, names
    assert {r["confidence"] for r in a["arrays"]} >= {"high", "medium"}


def test_every_single_array_result_is_byte_identical():
    """The /exchange/demand/{id}/draft-offtaker route calls array_vacancy directly
    with NO prefetch — that path must be untouched, array by array."""
    with SessionLocal() as db:
        ids = [r.id for r in db.query(Array).filter(Array.tenant_id == TID)
               .order_by(Array.id).all()]
    assert len(ids) >= 12

    compared = 0
    for aid in ids:
        with SessionLocal() as db_old:
            o = old.array_vacancy(db_old, db_old.get(Array, aid))
        with SessionLocal() as db_new:
            n = array_vacancy(db_new, db_new.get(Array, aid))
        assert json.dumps(o, sort_keys=True) == json.dumps(n, sort_keys=True), aid
        compared += 1
    assert compared >= 12


def test_multi_account_array_measures_only_the_min_id_host():
    """MIN(id) GROUP BY must pick exactly the row ORDER BY id LIMIT 1 picked —
    the two sibling accounts carry 99,999 kWh/mo that must stay invisible."""
    with SessionLocal() as db:
        arr = db.query(Array).filter(Array.tenant_id == TID,
                                     Array.name == "MultiAccount").one()
        accts = sorted(a.id for a in db.query(UtilityAccount)
                       .filter(UtilityAccount.array_id == arr.id).all())
        assert len(accts) == 3
        v = array_vacancy(db, arr)
        pre = _prefetch_vacancy(db, [arr])
    assert v["host_account_id"] == accts[0]
    assert pre.host_id_by_array[arr.id] == accts[0]
    assert v["pool_kwh"] < 200_000, v["pool_kwh"]   # 9 × 18k, not 9 × 99,999


# ── 2. THE PREFETCH IS INERT ──────────────────────────────────────────────────

def test_prefetch_and_no_prefetch_agree_array_by_array():
    with SessionLocal() as db:
        arrays = db.query(Array).filter(Array.tenant_id == TID).order_by(Array.id).all()
        pre = _prefetch_vacancy(db, arrays)
        for arr in arrays:
            with_pre = array_vacancy(db, arr, prefetch=pre)
            without = array_vacancy(db, arr)
            assert json.dumps(with_pre, sort_keys=True) == \
                   json.dumps(without, sort_keys=True), arr.name


def test_a_prefetch_for_other_arrays_is_ignored_not_misapplied():
    """`covers()` is the contract. A prefetch built for the WRONG arrays must fall
    back to the live queries — never answer with another array's rows, and never
    report an array as accountless just because it is missing from the maps."""
    with SessionLocal() as db:
        mine = db.query(Array).filter(Array.tenant_id == TID,
                                      Array.name == "Banker").one()
        theirs = db.query(Array).filter(Array.tenant_id == OTHER_TID).all()
        foreign = _prefetch_vacancy(db, theirs)
        assert not foreign.covers(mine.id)
        assert json.dumps(array_vacancy(db, mine, prefetch=foreign), sort_keys=True) == \
               json.dumps(array_vacancy(db, mine), sort_keys=True)

        # An empty prefetch is the degenerate case of the same thing.
        empty = VacancyPrefetch(frozenset(), {}, {}, {}, {})
        assert json.dumps(array_vacancy(db, mine, prefetch=empty), sort_keys=True) == \
               json.dumps(array_vacancy(db, mine), sort_keys=True)


# ── the one deliberate behaviour change: tied period_ends ────────────────────

def test_a_split_tie_keeps_the_FRESHEST_capture():
    """The one deliberate behaviour change, pinned to the semantic that justifies it.

    `period_end DESC` alone is NOT a total order, and `array_vacancy` then slices
    `bills[:window_months]` — so on a host account with tied months, WHICH tied
    bill survived the cut was the planner's choice. A prefetch cannot be provably
    equivalent over a non-total order, so the query is now pinned with
    `Bill.id.desc()`.

    That pin is not arbitrary. In prod every one of the 1,137 tied groups is a
    RE-CAPTURE of the same month (same kWh, pulled twice), and in 1,134 of them
    (99.7%, zero counterexamples) the higher id carries the LATER `pulled_at`.
    `id DESC` therefore sorts the FRESHEST capture of a tied month FIRST — so
    when the window cut lands mid-tie, the capture that survives is the newest
    one, not whichever the planner happened to emit.

    This test builds exactly that split (13 bills, cut at 12, landing inside the
    oldest month's tie) and asserts both halves: the result is stable across
    Sessions, AND the surviving bill is the fresh capture."""
    tid = "ten_vactie_" + secrets.token_hex(3)
    STALE = (40, 10.4)      # $0.26/kWh  — captured a month ago
    FRESH = (40, 7.36)      # $0.184/kWh — re-captured yesterday, higher id
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=secrets.token_hex(8), name=tid,
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = _mk_array(db, tid, "Tied")
        acct = _mk_account(db, tid, arr.id, "host")
        base = _now() - timedelta(days=3)
        for i in range(7):
            pe = base - timedelta(days=30 * i)
            # Month 0 is captured ONCE; months 1..6 twice. That makes 13 bills, so
            # the 12-row window cut falls INSIDE month 6's tied pair — the only
            # arrangement where the tiebreaker can actually decide anything.
            caps = (FRESH,) if i == 0 else (STALE, FRESH)
            for dup, (ckwh, cusd) in enumerate(caps):
                db.add(Bill(tenant_id=tid, account_id=acct.id,
                            period_start=pe - timedelta(days=29), period_end=pe,
                            kwh_generated=9000, kwh_sent_to_grid=5000.0,
                            solar_credit_usd=None, is_net_metered=True,
                            # the later capture is inserted second → higher id,
                            # exactly as a real re-capture lands
                            pulled_at=_now() - timedelta(days=30 - dup * 29),
                            raw_json=_excess_json(shared_kwh=1000,
                                                  credited_kwh=ckwh,
                                                  credited_usd=cusd)))
        _mk_subs(db, tid, arr.id, [0.3])
        db.commit()
        aid = arr.id

    stale_rate = round(STALE[1] / STALE[0], 5)
    fresh_rate = round(FRESH[1] / FRESH[0], 5)
    assert stale_rate != fresh_rate

    results = []
    for _ in range(5):
        with SessionLocal() as db:
            results.append(array_vacancy(db, db.get(Array, aid)))

    # Deterministic across fresh Sessions...
    assert len({json.dumps(r, sort_keys=True) for r in results}) == 1, results[0]
    assert results[0]["months_of_history"] == 12
    # ...and the bill that survived the split is the FRESH capture, not the stale
    # one. `credit_rate` is the last-iterated (oldest-in-window) bill's rate, which
    # in this construction IS the survivor of the split tie.
    assert results[0]["credit_rate"] == pytest.approx(fresh_rate), (
        results[0]["credit_rate"], "fresh", fresh_rate, "stale", stale_rate)


# ── 3. N+1 IS DEAD ────────────────────────────────────────────────────────────

def test_sql_statement_count_is_constant_as_the_fleet_grows():
    """The whole point. Two fleets of identical SHAPE and different SIZE must cost
    the same number of round trips."""
    small = _seed_fleet("ten_vacsmall_" + secrets.token_hex(3), n_filler=0)
    big = _seed_fleet("ten_vacbig_" + secrets.token_hex(3), n_filler=20)

    counts = {}
    for label, tid in (("small", small), ("big", big)):
        with SessionLocal() as db, count_sql() as c:
            out = tenant_vacancy(db, tid)
            counts[label] = c.n
        assert out["totals"]["array_count"] >= 10

    assert counts["small"] == counts["big"], counts
    # 1 arrays SELECT + 4 prefetch + the per-Session rate memos.
    assert counts["big"] <= 10, (counts, "expected a small constant")


def test_the_old_code_was_linear_in_array_count():
    """Anchors the claim: the same two fleets under the pre-patch implementation
    cost ~5 queries per array more. Without this the constant above proves only
    that the new code is constant, not that anything was actually removed."""
    small = _seed_fleet("ten_vacoldsm_" + secrets.token_hex(3), n_filler=0)
    big = _seed_fleet("ten_vacoldbg_" + secrets.token_hex(3), n_filler=20)

    counts = {}
    for label, tid in (("small", small), ("big", big)):
        with SessionLocal() as db, count_sql() as c:
            old.tenant_vacancy(db, tid)
            counts[label] = c.n

    grew_by = counts["big"] - counts["small"]
    assert grew_by >= 20 * 4, (counts, "pre-patch cost should scale with arrays")


# ── 4. NO LEAKAGE ─────────────────────────────────────────────────────────────

def test_the_prefetch_stays_inside_the_tenant():
    with SessionLocal() as db:
        mine = tenant_vacancy(db, TID)
        theirs = tenant_vacancy(db, OTHER_TID)
    mine_ids = {r["array_id"] for r in mine["arrays"]}
    their_ids = {r["array_id"] for r in theirs["arrays"]}
    assert mine_ids and their_ids and not (mine_ids & their_ids)
    with SessionLocal() as db:
        for aid in mine_ids:
            assert db.get(Array, aid).tenant_id == TID
