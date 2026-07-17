"""The Chint harvester must NOT count a data-logger/collector/detector as an inverter.

Regression for Bruce's Londonderry 186: the FlexOM FG4C logger (hex serial
00009e021902bb00, no model, never produces) was slipping into the inverter list via
the `assetType == 2` fallback and getting flagged as a dead inverter. See
docs/knowledge/chint-portal-api-contract.md — "1 gateway (assetType 1) → 4 inverters".
"""
from api.harvester.vendors.chint import ChintVendor, _is_inverter_device


def test_is_inverter_device_rejects_non_inverter_kinds():
    gw = {"sn": "00009e021902bb00"}
    # named non-inverter kinds — rejected regardless of assetType
    assert _is_inverter_device({"assetTypeName": "Detector", "sn": "00009e021902bb00"}, gw) is False
    assert _is_inverter_device({"assetTypeName": "Collector", "sn": "X"}, gw) is False
    assert _is_inverter_device({"assetTypeName": "DataLogger", "sn": "X"}, gw) is False
    assert _is_inverter_device({"assetTypeName": "Meter", "assetType": 2, "sn": "X"}, gw) is False
    # gateway by asset-type int
    assert _is_inverter_device({"assetType": 1, "sn": "X"}, gw) is False
    # the logger echoed as a commDevice under its own gateway (serial == gateway serial)
    assert _is_inverter_device({"sn": "00009e021902bb00", "assetType": 2}, gw) is False
    # real inverters pass — by name and by the assetType==2 fallback
    assert _is_inverter_device({"assetTypeName": "Inverter", "sn": "0001013791738041"}, gw) is True
    assert _is_inverter_device({"assetType": 2, "sn": "0001013791737108"}, gw) is True


def test_inverters_excludes_logger_keeps_four_real():
    devices = {"id": "S1", "gwDevices": [{
        "sn": "00009e021902bb00",
        "commDevices": [
            {"assetTypeName": "Detector", "sn": "00009e021902bb00", "statusName": "Normal"},
            {"assetTypeName": "Inverter", "sn": "0001013791738041", "model": "SCA50KTL-DO/US-480", "currentPower": 51000, "eToday": 98.6, "statusName": "Normal"},
            {"assetTypeName": "Inverter", "sn": "0001013791737108", "model": "SCA50KTL-DO/US-480", "currentPower": 51000, "eToday": 105.6, "statusName": "Normal"},
            {"assetTypeName": "Inverter", "sn": "0001013791738043", "model": "SCA50KTL-DO/US-480", "currentPower": 51000, "eToday": 101.3, "statusName": "Normal"},
            {"assetTypeName": "Inverter", "sn": "0001014081838033", "model": "SC36KTL-DO/US-480", "currentPower": 35000, "eToday": 73.5, "statusName": "Normal"},
        ],
    }]}
    inv = ChintVendor._inverters(devices)
    assert len(inv) == 4
    assert all(i["serial"] != "00009e021902bb00" for i in inv)
