"""Day-accurate commissioning date → GMP rate regime (Bruce's C4 ask).

Bruce: "You need to get more specific with the date to determine if 11 yo or
not" — GMP Rate #1 applies for the first 11 years from the commissioning DATE,
and a year-granular value can misclassify an array near the boundary (GMP has
called an array 11 two years early). Covers:
  • the day-before / day-after 11-year boundary,
  • the mid-month anniversary straddle (month keeps Rate #1, flagged),
  • legacy year-only values (assumed Jan 1 == the old whole-year math, so
    nothing flips on deploy),
  • blank commissioning info,
  • the PATCH /arrays/{id} full-date save + setup-state round-trip.
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from api.db import SessionLocal
from api.models import Tenant, Client, Array, UtilityAccount
from api.rate_schedule_gmp import age_years_on, blended_start, expected_gmp_rate


# ---- pure boundary math -----------------------------------------------------

def test_age_ticks_on_the_anniversary_day():
    cd = date(2015, 6, 15)
    assert age_years_on(cd, date(2026, 6, 14)) == 10   # day before the mark
    assert age_years_on(cd, date(2026, 6, 15)) == 11   # the 11-year day itself
    assert age_years_on(cd, date(2026, 6, 16)) == 11   # day after
    assert age_years_on(cd, date(2015, 6, 15)) == 0
    assert age_years_on(cd, date(2010, 1, 1)) == 0     # before commissioning → 0
    assert blended_start(cd) == date(2026, 6, 15)


def test_regime_day_accurate_around_11y_boundary():
    cd = date(2015, 6, 15)
    # Month before the anniversary month → squarely Rate #1.
    r = expected_gmp_rate(2026, 5, commission_date=cd)
    assert r["regime"] == "rate1"
    assert r["regime_flips_within_month"] is False
    assert r["blended_from"] == "2026-06-15"
    assert r["commission_date"] == "2015-06-15"
    assert r["year_only_assumed_jan1"] is False
    # The anniversary month itself: start-of-month regime (Rate #1 — never flip
    # early, which is exactly the GMP mistake Bruce has seen) + straddle flag.
    r = expected_gmp_rate(2026, 6, commission_date=cd)
    assert r["regime"] == "rate1"
    assert r["age_years"] == 10
    assert r["regime_flips_within_month"] is True
    # First full month past the anniversary → Blended.
    r = expected_gmp_rate(2026, 7, commission_date=cd)
    assert r["regime"] == "blended"
    assert r["age_years"] == 11
    assert r["regime_flips_within_month"] is False


def test_legacy_year_only_matches_old_whole_year_math():
    # Old behavior: age = target_year - commission_year. 2015 → age 11 for ALL
    # of 2026, age 10 for all of 2025. The Jan-1 assumption reproduces that
    # exactly, so legacy year-only arrays don't silently change regime.
    r = expected_gmp_rate(2025, 12, commission_year=2015)
    assert r["regime"] == "rate1"
    assert r["age_years"] == 10
    r = expected_gmp_rate(2026, 1, commission_year=2015)
    assert r["regime"] == "blended"
    assert r["age_years"] == 11
    r = expected_gmp_rate(2026, 6, commission_year=2015)
    assert r["regime"] == "blended"
    # Honesty flags: the date was assumed, not known.
    assert r["year_only_assumed_jan1"] is True
    assert r["commission_date"] is None
    assert r["blended_from"] == "2026-01-01"
    # A Jan-1 anniversary covers the whole month — no mid-month straddle.
    assert r["regime_flips_within_month"] is False


def test_full_date_beats_legacy_year_when_both_given():
    # commission_date is authoritative; commission_year is ignored beside it.
    r = expected_gmp_rate(2026, 6, commission_year=2010,
                          commission_date=date(2020, 6, 1))
    assert r["regime"] == "rate1"
    assert r["age_years"] == 6
    assert r["year_only_assumed_jan1"] is False


def test_blank_commissioning_defaults_rate1_with_no_boundary():
    r = expected_gmp_rate(2026, 6)
    assert r["regime"] == "rate1"
    assert r["age_years"] is None
    assert r["blended_from"] is None
    assert r["regime_flips_within_month"] is False


def test_leap_day_commissioning():
    cd = date(2016, 2, 29)
    assert blended_start(cd) == date(2027, 2, 28)      # 2027 isn't a leap year
    r = expected_gmp_rate(2027, 2, commission_date=cd)
    assert r["regime"] == "rate1"                      # Feb 1 2027 → still age 10
    assert r["regime_flips_within_month"] is True
    r = expected_gmp_rate(2027, 3, commission_date=cd)
    assert r["regime"] == "blended"


# ---- endpoint round-trip ----------------------------------------------------

def _seed():
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="CommDate Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="CD", active=True); db.add(c); db.flush()
        arr = Array(tenant_id=tid, name="CommDate Array", client_id=c.id,
                    fuel_type="solar", region="central"); db.add(arr); db.flush()
        db.add(UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="CD1", enabled=True))
        db.commit()
        return tid, arr.id


def _auth(tid):
    from api.account import mint_session_for_tenant
    return "Bearer " + mint_session_for_tenant(tid)


def test_full_date_save_roundtrip(client):
    tid, aid = _seed()
    auth = _auth(tid)
    # Save a full commissioning date — the day must persist, not just the year.
    r = client.patch(f"/v1/array-operator/billing/arrays/{aid}",
                     json={"first_connect_date": "2015-06-15"},
                     headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    assert r.json()["first_connect_date"] == "2015-06-15"
    d = client.get("/v1/array-operator/billing/setup-state",
                   headers={"Authorization": auth}).json()
    a = next(x for x in d["arrays"] if x["array_id"] == aid)
    assert a["first_connect_date"] == "2015-06-15"
    assert a["install_year"] == 2015
    assert a["age_known"] is True


def test_full_date_save_validates_sane_range(client):
    tid, aid = _seed()
    auth = _auth(tid)
    for bad in ("1989-12-31",
                (date.today() + timedelta(days=1)).isoformat(),
                "not-a-date"):
        r = client.patch(f"/v1/array-operator/billing/arrays/{aid}",
                         json={"first_connect_date": bad},
                         headers={"Authorization": auth})
        assert r.status_code == 400, f"{bad}: {r.status_code} {r.text}"
    # Legacy year path still works (Jan 1 of that year).
    r = client.patch(f"/v1/array-operator/billing/arrays/{aid}",
                     json={"install_year": 2018}, headers={"Authorization": auth})
    assert r.status_code == 200
    assert r.json()["first_connect_date"] == "2018-01-01"


def test_expected_rate_endpoint_accepts_commission_date(client):
    tid, _aid = _seed()
    auth = _auth(tid)
    base = "/v1/array-operator/billing/gmp-expected-rate"
    r = client.get(base + "?year=2026&month=6&commission_date=2015-06-15",
                   headers={"Authorization": auth})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["regime"] == "rate1"
    assert d["regime_flips_within_month"] is True
    assert d["blended_from"] == "2026-06-15"
    r = client.get(base + "?year=2026&month=7&commission_date=2015-06-15",
                   headers={"Authorization": auth})
    assert r.json()["regime"] == "blended"
    # Malformed date → 400, not a silent rate1.
    r = client.get(base + "?year=2026&month=6&commission_date=June-2015",
                   headers={"Authorization": auth})
    assert r.status_code == 400
    # Legacy year-only callers keep working.
    r = client.get(base + "?year=2026&month=6&commission_year=2020",
                   headers={"Authorization": auth})
    assert r.status_code == 200
    assert r.json()["regime"] == "rate1"
    assert r.json()["year_only_assumed_jan1"] is True
