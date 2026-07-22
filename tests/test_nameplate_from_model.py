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


def test_fronius_model_kw():
    # The model names the AC kW between the family and the phase digit. Bruce's
    # Chester (Primo 7.6) + Waterford (Primo 12.5) inverters showed a blank size
    # until this parsed. The "208-240" voltage must NOT be read as the rating.
    assert npm("fronius", "Primo 7.6-1 208-240") == 7.6
    assert npm("fronius", "Primo 12.5-1 208-240") == 12.5
    assert npm("fronius", "Primo 15.0-1 208/240") == 15.0
    assert npm("fronius", "Symo 24.0-3 480") == 24.0
    assert npm("fronius", "Symo GEN24 8.0-3 400") == 8.0
    # Solar.web 'NNNNN WDC Primo NN XX.X' — AC kW is the trailing rating, NOT
    # the leading DC Wp (Lester Middlebury → $17k/mo bug, Ford 2026-07-22).
    assert npm("fronius", "21330 WDC Primo 01 15.0") == 15.0
    assert npm("fronius", "17775 WDC Primo 04 12.5 ") == 12.5
    assert npm("fronius", "17775 WDC Primo 02 12.5-1 ") == 12.5
    assert npm("fronius", "10665 WDC Primo 10 7.6") == 7.6
    # Unknown / no rating → blank (not a guess).
    assert npm("fronius", "Primo 208-240") is None
    assert npm("fronius", "SE10000") is None          # not a Fronius model string
    assert npm("fronius", "0001013791738041") is None


def test_eff_nameplate_rejects_absurd_stored_wdc_watts():
    from types import SimpleNamespace
    from api.inverter_fleet import _eff_nameplate_kw
    iv = SimpleNamespace(
        nameplate_kw=21330.0, vendor="fronius",
        model="21330 WDC Primo 01 15.0", name="21330 WDC Primo 01 15.0",
    )
    assert _eff_nameplate_kw(iv, {}) == 15.0


def test_other_vendors_not_parsed():
    # SMA reports its own nameplate; a non-Fronius model on 'sma'/None stays blank.
    assert npm("sma", "SE5000") is None
    assert npm(None, "SE10000") is None
    assert npm(None, "Primo 7.6-1 208-240") is None   # needs vendor=='fronius'
