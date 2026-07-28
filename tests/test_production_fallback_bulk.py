"""compute_production_fallback_bulk must equal compute_production_fallback.

The bulk version replaced four SELECTs per array on /v1/array-owners/overview
with two total. It is a pure read optimization, so the ONLY thing that matters
is exact output parity — including on the edges that prod does not exercise.

Live prod validated 1055/1055 live arrays identical (all 5 active=True and all
13 source-bearing arrays included). These cover what prod has no instance of:
the 400-row / 60-day caps, whitespace-padded sources, two utility sources in
one window, and NULL sources.

Why classification stays in Python: str.strip() strips \\t\\n\\r\\f\\v, SQL
trim() strips only spaces. A source stored as 'chint\\n' would be a vendor row
to one and not the other, flipping active/source/days_filled/vendor_last_day
together. test_padded_and_newline_sources is the regression guard for that.
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from api.db import SessionLocal
from api.models import Array, DailyGeneration, Tenant, now
from api import production_fallback as pf

TODAY = date(2026, 7, 20)


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="PF Bulk", contact_email=f"{tid}@t.test",
            tenant_key="sol_test_" + secrets.token_hex(8),
            plan="standard", active=True,
        ))
        db.commit()
    return tid


def _array(tid: str, rows: list[tuple[date, float, str | None]]) -> int:
    """One array seeded with (day, kwh, source) rows."""
    with SessionLocal() as db:
        arr = Array(tenant_id=tid, name="A-" + secrets.token_hex(3),
                    fuel_type="solar")
        db.add(arr)
        db.flush()
        for day, kwh, src in rows:
            db.add(DailyGeneration(
                tenant_id=tid, array_id=arr.id, day=day, kwh=kwh,
                source=src, uploaded_at=now(),
            ))
        aid = arr.id
        db.commit()
    return aid


def _assert_parity(aids: list[int]) -> dict:
    """Bulk over the whole set must equal per-array, array by array."""
    with SessionLocal() as db:
        singles = {a: pf.compute_production_fallback(db, a, today=TODAY)
                   for a in aids}
        bulk = pf.compute_production_fallback_bulk(db, aids, today=TODAY)
    assert set(bulk) == set(aids), "bulk must return an entry per input id"
    for a in aids:
        assert singles[a] == bulk[a], f"array {a}: {singles[a]} != {bulk[a]}"
    return singles


# ── the four scenarios from test_production_fallback, batched together ───────

def test_parity_across_the_four_canonical_scenarios():
    tid = _tenant()
    # 1. vendor alive (positive yesterday) → not dead, not active
    alive = _array(tid, [
        (TODAY - timedelta(days=1), 120.0, "extension_pull"),
        (TODAY, 0.0, "extension_pull"),
    ])
    # 2. vendor dead + utility carrying → active
    dead_filled = _array(tid, [
        (TODAY - timedelta(days=30), 300.0, "extension_pull"),
        (TODAY - timedelta(days=2), 0.0, "extension_pull"),
        (TODAY - timedelta(days=1), 210.0, "utility_meter"),
        (TODAY, 190.0, "utility_meter"),
    ])
    # 3. vendor dead, NO utility → dead but nothing to fall back to
    dead_bare = _array(tid, [
        (TODAY - timedelta(days=30), 300.0, "extension_pull"),
        (TODAY, 0.0, "extension_pull"),
    ])
    # 4. no rows at all
    empty = _array(tid, [])

    out = _assert_parity([alive, dead_filled, dead_bare, empty])
    # The batch really did exercise the interesting states.
    assert out[alive]["active"] is False
    assert out[dead_filled]["active"] is True
    assert out[dead_filled]["days_filled"] == 2
    assert out[dead_bare]["active"] is False
    assert out[empty] == {"active": False, "source": None,
                          "days_filled": 0, "vendor_last_day": None}


# ── edges prod has zero instances of ─────────────────────────────────────────

def test_vendor_rows_beyond_the_400_row_cap():
    """The 400-row LIMIT must survive the rewrite to row_number()."""
    tid = _tenant()
    rows: list[tuple[date, float, str | None]] = [
        (TODAY - timedelta(days=i), 5.0, "utility_meter") for i in range(410)
    ]
    # A positive vendor day is buried at offset 405 — outside the cap, so
    # BOTH implementations must fail to see it (vendor_last_day stays None).
    rows[405] = (TODAY - timedelta(days=405), 99.0, "extension_pull")
    aid = _array(tid, rows)
    out = _assert_parity([aid])
    assert out[aid]["vendor_last_day"] is None, "row past the cap leaked in"


def test_utility_label_beyond_the_60_day_cap():
    """_utility_source_label's LIMIT 60 must survive too."""
    tid = _tenant()
    rows: list[tuple[date, float, str | None]] = [
        (TODAY - timedelta(days=i), 0.0, "extension_pull") for i in range(70)
    ]
    rows[65] = (TODAY - timedelta(days=65), 40.0, "smarthub")
    aid = _array(tid, rows)
    out = _assert_parity([aid])
    assert out[aid]["source"] is None, "utility past the 60-row cap leaked in"


def test_two_utility_sources_in_the_window():
    """Most recent utility day wins the label — explicit max, not row order."""
    tid = _tenant()
    aid = _array(tid, [
        (TODAY - timedelta(days=40), 100.0, "extension_pull"),
        (TODAY - timedelta(days=5), 50.0, "smarthub"),
        (TODAY - timedelta(days=1), 60.0, "gmp_api"),
    ])
    _assert_parity([aid])


def test_padded_and_newline_sources():
    """THE reason classification stays in Python: SQL trim() != str.strip()."""
    tid = _tenant()
    aid = _array(tid, [
        (TODAY - timedelta(days=40), 100.0, "  EXTENSION_PULL\n"),
        (TODAY - timedelta(days=2), 0.0, "chint\n"),
        (TODAY - timedelta(days=1), 75.0, "\tUtility_Meter "),
        (TODAY, 80.0, "utility_meter"),
    ])
    out = _assert_parity([aid])
    # Padded vendor rows were really recognised as vendor rows.
    assert out[aid]["vendor_last_day"] == (TODAY - timedelta(days=2)).isoformat()


def test_null_source_rows():
    tid = _tenant()
    aid = _array(tid, [
        (TODAY - timedelta(days=3), 10.0, None),
        (TODAY - timedelta(days=1), 20.0, None),
    ])
    _assert_parity([aid])


def test_vendor_zeros_only_never_counts_as_alive():
    """Zeros do not keep a feed alive (a broken portal writes 0s)."""
    tid = _tenant()
    aid = _array(tid, [
        (TODAY - timedelta(days=1), 0.0, "chint"),
        (TODAY, 0.0, "chint"),
    ])
    out = _assert_parity([aid])
    assert out[aid]["vendor_last_day"] == TODAY.isoformat()


def test_arrays_stay_separate_in_one_batch():
    """Regrouping must not smear one array's days onto another."""
    tid = _tenant()
    a1 = _array(tid, [(TODAY - timedelta(days=1), 210.0, "utility_meter"),
                      (TODAY - timedelta(days=40), 5.0, "chint")])
    a2 = _array(tid, [(TODAY - timedelta(days=1), 500.0, "extension_pull")])
    out = _assert_parity([a1, a2])
    assert out[a1] != out[a2]


def test_empty_input():
    with SessionLocal() as db:
        assert pf.compute_production_fallback_bulk(db, [], today=TODAY) == {}
        assert pf.compute_production_fallback_bulk(db, None, today=TODAY) == {}
