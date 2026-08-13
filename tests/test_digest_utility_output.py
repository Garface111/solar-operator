"""Morning digest: utility-metered output weave (Paul's daily-email ask).

Paul (HCT), 2026-08-13: "could the system look to the VEC records to provide
the previous day's output and GMP for the Norwich array … the Fronius and
Chint is really about system health … the GMP/VEC data can give you
yesterday's output."

Two behaviors, two blast radii:

1. FLEET-WIDE (no flag): an array already in the digest whose inverter feed is
   stale gets its output figure from the freshest stream — the utility meter
   wins ties — with a provenance chip ("GMP meter"). Health/flags stay vendor.
2. PER-TENANT OPT-IN (digest_include_utility_arrays): zero-inverter metered
   arrays join the OUTPUT surfaces. Default OFF because fleets differ
   structurally: for HCT those arrays ARE the fleet; for GMCS the equivalent
   rows are customer credit accounts — noise (Bruce: "only show vendor data").
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from api.db import SessionLocal
from api.jobs import morning_fleet_digest as digest
from api.models import Array, DailyGeneration, Inverter, Tenant, UtilityAccount

TEN = "ten_util_digest_t1"


def _purge(db):
    """Remove everything this file persisted. The suite shares one DB and some
    tests assert GLOBAL emptiness (test_match_preview_saves_nothing pattern) —
    leaked rows here become another file's mystery failure."""
    for model in (DailyGeneration, Inverter, UtilityAccount, Array):
        for row in db.query(model).filter_by(tenant_id=TEN).all():
            db.delete(row)
    t = db.get(Tenant, TEN)
    if t is not None:
        db.delete(t)
    db.commit()


def _iso(days_ago: int) -> str:
    return (datetime.strptime(digest._local_today_iso(), "%Y-%m-%d")
            - timedelta(days=days_ago)).date().isoformat()


def _col(vendor_days=None, utility_days=None, providers=None, invs=1, name="A"):
    return {
        "array_id": 1, "array_name": name, "inverter_count": invs,
        "inverters": [], "alert": {"level": "ok", "count": 0}, "daily": [],
        "daily_split": {
            "vendor": [{"date": d, "kwh": k} for d, k in (vendor_days or [])],
            "utility": [{"date": d, "kwh": k} for d, k in (utility_days or [])],
        },
        **({"utility_providers": providers} if providers else {}),
    }


# ── the output point ────────────────────────────────────────────────────────

def test_vendor_fresher_keeps_the_vendor_number():
    c = _col(vendor_days=[(_iso(2), 90.0), (_iso(1), 100.0)],
             utility_days=[(_iso(3), 95.0), (_iso(2), 96.0)], providers=["gmp"])
    pt = digest._output_point(c)
    assert pt["stream"] == "vendor" and pt["kwh"] == 100.0 and pt["chip"] is None


def test_utility_fresher_wins_with_a_provenance_chip():
    """Danville while its Fronius capture is down: vendor stuck a week back,
    GMP meter current — the owner gets the metered figure, labeled."""
    c = _col(vendor_days=[(_iso(8), 400.0)],
             utility_days=[(_iso(2), 950.0), (_iso(1), 1010.0)], providers=["gmp"])
    pt = digest._output_point(c)
    assert pt["stream"] == "utility"
    assert pt["kwh"] == 1010.0
    assert pt["chip"] == "GMP meter"


def test_same_day_tie_goes_to_the_settled_meter():
    c = _col(vendor_days=[(_iso(1), 980.0)],
             utility_days=[(_iso(1), 1000.0)], providers=["vec"])
    pt = digest._output_point(c)
    assert pt["stream"] == "utility" and pt["chip"] == "VEC meter"


def test_trailing_partial_meter_day_is_trimmed():
    """GMP interval data lands progressively: the newest day can hold a sliver
    (1.62 kWh at 05:00 for a ~1 MWh site). The complete-day picker's partial
    guard applies to the utility stream too — never report the sliver."""
    utility = [(_iso(5), 990.0), (_iso(4), 1012.0), (_iso(3), 1073.0),
               (_iso(2), 1025.0), (_iso(1), 1.62)]
    c = _col(utility_days=utility, providers=["gmp"], invs=0)
    pt = digest._output_point(c)
    assert pt["kwh"] == 1025.0 and pt["date"] == _iso(2)


def test_meter_one_day_behind_is_current_a_dead_login_is_not():
    yiso = digest._yesterday_iso()
    fresh_meter = {"date": _iso(2), "kwh": 5.0, "stream": "utility"}
    dead_meter = {"date": _iso(9), "kwh": 5.0, "stream": "utility"}
    lagging_vendor = {"date": _iso(2), "kwh": 5.0, "stream": "vendor"}
    assert digest._point_is_current(fresh_meter, yiso), (
        "a meter settles ~a day behind — that is the feed working normally")
    assert not digest._point_is_current(dead_meter, yiso)
    assert not digest._point_is_current(lagging_vendor, yiso), (
        "vendor telemetry has no settlement lag — a day behind IS stale")


# ── attach_utility_columns (DB) ─────────────────────────────────────────────

def _seed(db, *, flag: bool, tag: str = ""):
    t = db.get(Tenant, TEN)
    if t is None:
        t = Tenant(id=TEN, name="Util Digest Co", contact_email="u@example.com",
                   tenant_key=f"key_{TEN}", active=True, is_demo=True)
        db.add(t)
        db.flush()
    t.digest_include_utility_arrays = flag

    def _array(name):
        a = Array(tenant_id=TEN, name=name)
        db.add(a)
        db.flush()
        return a

    metered = _array(f"Norwich Union Village{tag}")  # zero-inverter, metered
    vendor = _array(f"Danville Big Buck{tag}")       # has an inverter
    estimated = _array(f"Bill Prorate Only{tag}")    # only bill estimates

    db.add(Inverter(tenant_id=TEN, array_id=vendor.id, vendor="fronius",
                    serial=f"fx1{tag}", name="Inv 1", position=1))
    for arr, prov, acct in ((metered, "gmp", "1001"), (vendor, "gmp", "1002"),
                            (estimated, "gmp", "1003")):
        db.add(UtilityAccount(tenant_id=TEN, array_id=arr.id, provider=prov,
                              account_number=f"{acct}{tag}"))
    today = date.today()
    for i in (1, 2, 3):
        db.add(DailyGeneration(tenant_id=TEN, array_id=metered.id,
                               day=today - timedelta(days=i),
                               kwh=1000.0 + i, source="utility_meter"))
        db.add(DailyGeneration(tenant_id=TEN, array_id=estimated.id,
                               day=today - timedelta(days=i),
                               kwh=500.0, source="bill_prorate"))
    db.commit()
    return metered, vendor, estimated


def _tree_for(vendor_array):
    return {"columns": [{
        "array_id": vendor_array.id, "array_name": vendor_array.name,
        "inverter_count": 1, "inverters": [],
        "alert": {"level": "ok", "count": 0}, "daily": [],
        "daily_split": {"vendor": [], "utility": []},
    }]}


def test_flag_off_attaches_providers_but_no_extra_columns():
    with SessionLocal() as db:
        try:
            metered, vendor, _ = _seed(db, flag=False, tag=" OFF")
            tree = _tree_for(vendor)
            digest.attach_utility_columns(db, db.get(Tenant, TEN), tree)
            assert tree["utility_columns"] == [], (
                "default off: Bruce's digest stays byte-identical")
            assert tree["columns"][0]["utility_providers"] == ["gmp"], (
                "the provenance chip needs providers on vendor columns fleet-wide")
        finally:
            _purge(db)


def test_flag_on_adds_metered_zero_inverter_arrays_only():
    with SessionLocal() as db:
      try:
        metered, vendor, estimated = _seed(db, flag=True, tag=" ON")
        tree = _tree_for(vendor)
        digest.attach_utility_columns(db, db.get(Tenant, TEN), tree)
        added = {c["array_name"]: c for c in tree["utility_columns"]}
        assert metered.name in added, "the metered zero-inverter array joins"
        assert vendor.name not in added, "vendor arrays already have a column"
        assert estimated.name not in added, (
            "bill_prorate is an estimate, not a metered feed — no honest row")
        col = added[metered.name]
        assert col["inverter_count"] == 0
        assert col["utility_providers"] == ["gmp"]
        assert col["daily_split"]["utility"], "carries the metered series"
        pt = digest._output_point(col)
        assert pt["stream"] == "utility" and pt["chip"] == "GMP meter"
      finally:
        _purge(db)


# ── rendering ───────────────────────────────────────────────────────────────

def _mk_tenant():
    return Tenant(id="t_render", name="Render Co", contact_email="r@example.com",
                  tenant_key="key_render", active=True)


def test_html_renders_metered_rows_and_chips():
    tree = {
        "columns": [_col(vendor_days=[(_iso(1), 120.0)], invs=2, name="Vendor A")],
        "utility_columns": [_col(utility_days=[(_iso(2), 990.0), (_iso(1), 1010.0)],
                                 providers=["gmp"], invs=0, name="Norwich")],
        "connection_health": {"ok": True, "problems": []},
    }
    html = digest.build_digest_html(_mk_tenant(), tree)
    assert "Norwich" in html
    assert "1,010.0 kWh" in html
    assert "GMP meter" in html
    assert "Utility-metered" in html
    text = digest.build_digest_text(_mk_tenant(), tree)
    assert "Norwich" in text and "GMP meter" in text


def test_without_utility_columns_nothing_changes():
    tree = {
        "columns": [_col(vendor_days=[(_iso(1), 120.0)], invs=2, name="Vendor A")],
        "connection_health": {"ok": True, "problems": []},
    }
    html = digest.build_digest_html(_mk_tenant(), tree)
    assert "Utility-metered" not in html
    assert "meter" not in digest.build_digest_text(_mk_tenant(), tree).lower()
