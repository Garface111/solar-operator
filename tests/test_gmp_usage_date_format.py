"""Regression: GMP's /usage/{acct}/download endpoint rejects a bare
'YYYY-MM-DD' date with HTTP 400 INVALID_DATE. The request MUST send full
ISO-8601 with milliseconds + 'Z'. Verified live 2026-07-06 — the format bug that
kept the daily-generation sponge empty and forced flat bill-proration in GMCS
reports (which diverged from Crown REC's monthly numbers).
"""
from datetime import date, datetime

from api.adapters import gmp


def test_helper_formats_date_as_iso_z():
    assert gmp._gmp_usage_date(date(2026, 2, 1)) == "2026-02-01T00:00:00.000Z"
    assert gmp._gmp_usage_date(date(2025, 12, 31)) == "2025-12-31T00:00:00.000Z"


def test_helper_preserves_datetime_time():
    assert gmp._gmp_usage_date(datetime(2025, 6, 1, 5, 45, 0)) == "2025-06-01T05:45:00.000Z"


def test_fetch_usage_csv_sends_iso_z_dates(monkeypatch):
    captured = {}

    class _FakeResp:
        status_code = 200
        text = "ServiceAgreement,IntervalStart,IntervalEnd,Quantity,UnitOfMeasure\n"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, params=None):
            captured["params"] = params or {}
            return _FakeResp()

    monkeypatch.setattr(gmp.httpx, "Client", _FakeClient)
    gmp.fetch_usage_csv("ACCT", "jwt", date(2025, 6, 1), date(2025, 7, 1))
    assert captured["params"]["startDate"] == "2025-06-01T00:00:00.000Z"
    assert captured["params"]["endDate"] == "2025-07-01T00:00:00.000Z"
    assert captured["params"]["format"] == "csv"
