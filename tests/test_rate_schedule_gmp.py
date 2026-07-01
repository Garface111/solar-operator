"""GMP published-rate lookup (api/rate_schedule_gmp.py).

Verifies the digitized GMP Rates 2026 schedule against the workbook's own
computed values, and the age→regime switch (Rate #1 pre-10-year-anniversary,
Blended Statewide after).
"""
from api.rate_schedule_gmp import expected_gmp_rate, regime_for_age, _rates


def test_data_file_loads_and_is_sane():
    d = _rates()
    assert abs(sum(d["insolation_weights"]) - 1.0) < 0.01
    assert d["solar_adder"] == 0.043
    assert "2026" in d["rate1"]["monthly"]
    assert "2026" in d["blended"]["monthly"]


def test_regime_switches_at_age_11():
    # Ford confirmed: <11 is Rate #1, 11+ is Blended Statewide.
    assert regime_for_age(0) == "rate1"
    assert regime_for_age(10) == "rate1"
    assert regime_for_age(11) == "blended"
    assert regime_for_age(15) == "blended"
    assert regime_for_age(None) == "rate1"   # unknown age → Rate #1 default


def test_rate1_2026_matches_workbook():
    # A 2020-commissioned array is 6yr old in 2026 → Rate #1.
    r = expected_gmp_rate(2026, 6, commission_year=2020)
    assert r["regime"] == "rate1"
    assert r["age_years"] == 6
    assert abs(r["weighted_avg_per_kwh"] - 0.216621) < 0.0005      # sheet 0.21662
    assert abs(r["weighted_avg_plus_adder"] - 0.259621) < 0.0005   # sheet 0.25962


def test_blended_when_past_anniversary():
    # A 2010-commissioned array is 16yr old in 2026 → Blended Statewide.
    r = expected_gmp_rate(2026, 6, commission_year=2010)
    assert r["regime"] == "blended"
    assert abs(r["weighted_avg_per_kwh"] - 0.186913) < 0.0005      # sheet 0.18691


def test_future_year_clamps_to_latest_published():
    r = expected_gmp_rate(2099, 6, commission_year=2098)  # age 1 → rate1
    assert r["regime"] == "rate1"
    assert r["clamped"] is True
    assert r["source_year_used"] == 2035
