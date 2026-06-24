"""Model-code → nameplate kW parsing (grounded "% of rated" denominator).

Vendors that don't report a per-unit nameplate still name the AC rating in the
model code. We parse it so the spreadsheet/card "% of rated" isn't blank.
"""
from __future__ import annotations

from api.inverter_fleet import _nameplate_from_model as npm


def test_solaredge_watt_models():
    # Residential SE#### = watts → kW. (Starlake's inverters in Ford's screenshot.)
    assert npm("solaredge", "SE10000") == 10.0
    assert npm("solaredge", "SE5000") == 5.0
    assert npm("solaredge", "SE7600") == 7.6
    assert npm("solaredge", "SE3000") == 3.0


def test_solaredge_with_hdwave_and_region_suffix():
    assert npm("solaredge", "SE10000H-US000BNU4") == 10.0
    assert npm("solaredge", "SE5000H-US000BNU4") == 5.0
    assert npm("solaredge", "SE7600H-US") == 7.6


def test_solaredge_commercial_kilowatt_models():
    # Three-phase commercial SE##K / SE##.#K = kW — must NOT be read as watts.
    assert npm("solaredge", "SE33.3KUS") == 33.3
    assert npm("solaredge", "SE100KUS") == 100.0
    assert npm("solaredge", "SE9KUS") == 9.0


def test_solaredge_unknown_returns_none():
    assert npm("solaredge", "Optimizer P850") is None
    assert npm("solaredge", "") is None
    assert npm("solaredge", None) is None


def test_chint_unchanged():
    assert npm("chint", "SCA50KTL-DO/US-480") == 50.0
    assert npm("chint", "SC36KTL-DO/US-480") == 36.0
    assert npm("chint", "random") is None


def test_other_vendors_not_parsed():
    # Fronius/SMA models aren't parsed here — they report nameplate or stay blank.
    assert npm("fronius", "SE10000") is None
    assert npm("sma", "SE5000") is None
    assert npm(None, "SE10000") is None
