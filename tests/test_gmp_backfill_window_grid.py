"""The sponge must stay finite: window boundaries are a FIXED grid.

Regression guard for the 2026-09-10 outage. `gmp_usage_raw` de-duplicates on
(account, window_start, window_end). The walk used to anchor its grid to
`date.today()`, so every boundary shifted a day per run, no window ever matched
the skip set, and each twice-daily run re-absorbed every account's full history
back to 2000. The sponge reached 21 GB, filled the 30 GB volume, and Postgres
crash-looped on "No space left on device" for eight days.
"""
from datetime import date, timedelta

from api.jobs.gmp_daily_backfill import (
    MIN_FLOOR_DATE,
    WINDOW_DAYS,
    plan_windows,
)


def _keys(today):
    return {(s, e) for s, e, _ in plan_windows(today)}


def test_window_keys_do_not_move_from_day_to_day():
    """The whole bug in one assertion: yesterday's keys must still be today's."""
    d = date(2026, 9, 18)
    assert _keys(d) == _keys(d + timedelta(days=1))


def test_history_is_absorbed_once_not_once_per_run():
    """Six months of twice-daily runs must not multiply the stored windows."""
    start = date(2026, 9, 18)
    seen = set()
    for i in range(180):
        seen |= _keys(start + timedelta(days=i))
    # One grid cell per `WINDOW_DAYS` from the floor to ~6 months out, and
    # nothing like the ~9,900 the sliding grid produced over the same span.
    assert len(seen) < 200


def test_windows_are_contiguous_with_no_gaps_or_overlap():
    w = sorted(plan_windows(date(2026, 9, 18)))
    for earlier, later in zip(w, w[1:]):
        assert earlier[1] == later[0]
    assert w[0][0] == MIN_FLOOR_DATE
    assert all(e - s == timedelta(days=WINDOW_DAYS) for s, e, _ in w)


def test_gmp_is_never_asked_for_a_future_range():
    today = date(2026, 9, 18)
    for _s, _e, fetch_end in plan_windows(today):
        assert fetch_end <= today + timedelta(days=1)


def test_only_the_live_cell_is_clamped():
    today = date(2026, 9, 18)
    clamped = [(s, e, f) for s, e, f in plan_windows(today) if f != e]
    assert len(clamped) == 1
    s, e, f = clamped[0]
    assert s <= today < e  # it is the cell containing today


def test_walk_starts_at_the_floor_and_never_pages_below_it():
    for s, _e, _f in plan_windows(date(2026, 9, 18)):
        assert s >= MIN_FLOOR_DATE
