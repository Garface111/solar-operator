"""A producing Fronius site whose Solar.web 'LastImport' lags must still show its
captured live power (Ford's "Waterford produces on Solar.web but no live feed here").
A site whose source genuinely stopped hours ago stays blanked; night stays blanked."""
from datetime import timedelta

from api.models import Inverter
from api import inverter_fleet as fleet


def _iv(**kw):
    iv = Inverter(tenant_id="t", array_id=1, vendor="fronius", serial="W1")
    for k, v in kw.items():
        setattr(iv, k, v)
    return iv


def test_producing_with_lagging_source_ts_shows_in_daylight():
    # Captured 48 min ago (hourly recapture); Solar.web LastImport ~1.9h ago; daytime.
    iv = _iv(last_power_w=6194.6,
             last_power_at=fleet.now() - timedelta(minutes=48),
             source_last_data_at=fleet.now() - timedelta(hours=1, minutes=54))
    assert fleet._live_power_w(iv, {}, daylight=True) == 6194.6


def test_source_stopped_hours_ago_is_blanked_even_in_daylight():
    # West Chester: source stopped ~7h ago -> blanked (and the SOURCE-OFFLINE banner
    # fires at the same 6h threshold, so number + banner agree).
    iv = _iv(last_power_w=1164.6,
             last_power_at=fleet.now() - timedelta(minutes=48),
             source_last_data_at=fleet.now() - timedelta(hours=7, minutes=18))
    assert fleet._live_power_w(iv, {}, daylight=True) is None


def test_captured_value_hidden_at_night():
    iv = _iv(last_power_w=6194.6,
             last_power_at=fleet.now() - timedelta(minutes=10),
             source_last_data_at=fleet.now() - timedelta(minutes=20))
    assert fleet._live_power_w(iv, {}, daylight=False) is None


def test_yesterdays_capture_blanked():
    # own_recent gate: a capture older than _POWER_FRESH (a day) is never "now".
    iv = _iv(last_power_w=6194.6,
             last_power_at=fleet.now() - timedelta(hours=26),
             source_last_data_at=fleet.now() - timedelta(hours=26))
    assert fleet._live_power_w(iv, {}, daylight=True) is None


def test_realtime_captured_power_shows_live():
    # After the GetActualPvSystemData fix: per-inverter power now comes from Solar.web's
    # REALTIME feed (fresh value + fresh source ts), not the lagging devwork chart that
    # left Waterford stuck OFFLINE on a stale near-zero. A fresh realtime capture shows
    # the real live watts.
    iv = _iv(last_power_w=12588.0,
             last_power_at=fleet.now() - timedelta(minutes=2),
             source_last_data_at=fleet.now() - timedelta(minutes=2))
    assert fleet._live_power_w(iv, {}, daylight=True) == 12588.0
