"""
Unit tests for api/adapters/smarthub.py.

Covers:
  - authenticate() with mocked httpx — cookies/token extracted, expires_at set
  - fetch_daily_generation() with mocked 30-day DAILY response — correct daily totals
  - fetch_daily_generation() using WEC host — confirms host substitution
  - 401 from auth endpoint → descriptive HTTPStatusError raised
  - Empty interval data → returns empty list, no crash
  - parse_extension_payload() — VEC, WEC, unknown host normalization
  - is_smarthub_provider() — true/false routing
  - parse_usage() and parse_bill() — parity with vec.py tests (same implementation)
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from api.adapters.smarthub import (
    ALL_SMARTHUB_PROVIDERS,
    authenticate,
    fetch_daily_generation,
    is_smarthub_provider,
    parse_bill,
    parse_extension_payload,
    parse_usage,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _mock_auth_response(token="tok_abc123", username="user@example.com"):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "authorizationToken": token,
        "primaryUsername": username,
    }
    return resp


def _mock_401():
    exc = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=MagicMock(),
        response=MagicMock(status_code=401),
    )
    return exc


def _daily_poll_response(days: list[date], kwh_per_day: float = 5.0) -> dict:
    """Fake POST /services/secured/utility-usage DAILY response.

    GROUNDED shape (live VEC West Glover HAR, Jun 2026): the response IS the data
    object directly — {"ELECTRIC": [ {series, meters, ...} ]} — with NO
    {"status":"COMPLETE","data":...} envelope and NO type=="USAGE" marker. This
    helper exercises the explicit RETURN(generation)+FORWARD(consumption) channel
    shape some deployments expose.
    """
    series_data = [
        {"x": int(datetime(d.year, d.month, d.day).timestamp() * 1000), "y": kwh_per_day}
        for d in days
    ]
    return {
        "ELECTRIC": [
            {
                "unitOfMeasure": "KWH",
                "meters": [
                    {"seriesId": "series_return", "flowDirection": "RETURN"},
                    {"seriesId": "series_forward", "flowDirection": "FORWARD"},
                ],
                "series": [
                    {"name": "series_return", "data": series_data},
                    {"name": "series_forward", "data": [
                        {"x": pt["x"], "y": 2.0} for pt in series_data
                    ]},
                ],
            }
        ]
    }


# ─── authenticate ─────────────────────────────────────────────────────────────

def test_authenticate_extracts_token_and_expiry():
    with patch("httpx.post", return_value=_mock_auth_response()) as mock_post:
        session = authenticate("vermontelectric.smarthub.coop", "user@example.com", "pw")

    assert session["auth_token"] == "tok_abc123"
    assert session["primary_username"] == "user@example.com"
    assert session["email"] == "user@example.com"
    assert isinstance(session["expires_at"], datetime)
    # expires_at should be in the near future (5 minutes)
    delta = (session["expires_at"] - datetime.utcnow()).total_seconds()
    assert 0 < delta < 400

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "vermontelectric.smarthub.coop" in call_kwargs.args[0]
    assert call_kwargs.kwargs["data"]["userId"] == "user@example.com"


def test_authenticate_wec_host():
    with patch("httpx.post", return_value=_mock_auth_response(token="tok_wec")) as mock_post:
        session = authenticate("washingtonelectric.smarthub.coop", "w@example.com", "pw")

    assert session["auth_token"] == "tok_wec"
    # Confirm the WEC host was used
    assert "washingtonelectric.smarthub.coop" in mock_post.call_args.args[0]


def test_authenticate_401_raises():
    with patch("httpx.post", side_effect=_mock_401()):
        with pytest.raises(httpx.HTTPStatusError):
            authenticate("vermontelectric.smarthub.coop", "bad@example.com", "wrong")


def test_authenticate_missing_token_raises():
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {}  # no authorizationToken
    with patch("httpx.post", return_value=resp):
        with pytest.raises(ValueError, match="missing authorizationToken"):
            authenticate("vermontelectric.smarthub.coop", "u@example.com", "pw")


# ─── fetch_daily_generation ───────────────────────────────────────────────────

def _session(host_code="vec"):
    return {
        "auth_token": "tok_test",
        "primary_username": "user@test.com",
        "email": "user@test.com",
        "expires_at": datetime(2099, 1, 1),
    }


def test_fetch_daily_generation_30_days():
    days = [date(2024, 3, d) for d in range(1, 31)]
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _daily_poll_response(days, kwh_per_day=10.0)

    with patch("httpx.post", return_value=mock_resp):
        result = fetch_daily_generation(
            host="vermontelectric.smarthub.coop",
            session=_session(),
            service_location="SL12345",
            account_number="6578300",
            start=date(2024, 3, 1),
            end=date(2024, 3, 30),
        )

    assert len(result) == 30
    for row in result:
        assert isinstance(row["day"], date)
        assert row["kwh_generated"] == pytest.approx(10.0)
        assert row["kwh_consumed"] == pytest.approx(2.0)
        assert row["kwh_net_export"] == pytest.approx(8.0)


def test_fetch_daily_generation_wec_host():
    days = [date(2024, 4, 1)]
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _daily_poll_response(days, kwh_per_day=3.5)

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        result = fetch_daily_generation(
            host="washingtonelectric.smarthub.coop",
            session=_session(),
            service_location="SLWEC001",
            account_number="9001",
            start=date(2024, 4, 1),
            end=date(2024, 4, 1),
        )

    assert len(result) == 1
    assert result[0]["kwh_generated"] == pytest.approx(3.5)
    # Confirm WEC host was used in the POST URL
    assert "washingtonelectric.smarthub.coop" in mock_post.call_args.args[0]


def test_fetch_daily_generation_empty_data():
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {}   # no ELECTRIC key → no rows

    with patch("httpx.post", return_value=mock_resp):
        result = fetch_daily_generation(
            host="vermontelectric.smarthub.coop",
            session=_session(),
            service_location="SL1",
            account_number="0",
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )

    assert result == []


def test_fetch_daily_generation_no_series():
    # ELECTRIC entry with no series/meters → no rows (honest empty).
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "ELECTRIC": [{"unitOfMeasure": "KWH", "series": [], "meters": []}],
    }

    with patch("httpx.post", return_value=mock_resp):
        result = fetch_daily_generation(
            host="vermontelectric.smarthub.coop",
            session=_session(),
            service_location="SL1",
            account_number="0",
            start=date(2024, 1, 1),
            end=date(2024, 1, 7),
        )

    assert result == []


def test_fetch_daily_generation_net_flow():
    """Net-metered VEC meter: NEGATIVE daily y = export/generation (the real West
    Glover signal), positive y = net consumption — regardless of flow flags."""
    net_data = [
        {"x": int(datetime(2024, 5, 1).timestamp() * 1000), "y": -8.0},  # exported 8 kWh
        {"x": int(datetime(2024, 5, 2).timestamp() * 1000), "y": 3.0},   # consumed 3 kWh
    ]
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "ELECTRIC": [
            {
                "unitOfMeasure": "KWH",
                # West Glover's meter is tagged FORWARD + isNetMeter=false yet
                # net-exports (negative values) — the sign is the source of truth.
                "meters": [{"seriesId": "65783", "flowDirection": "FORWARD", "isNetMeter": False}],
                "series": [{"name": "65783", "data": net_data}],
            }
        ],
    }

    with patch("httpx.post", return_value=mock_resp):
        result = fetch_daily_generation(
            host="vermontelectric.smarthub.coop",
            session=_session(),
            service_location="SL1",
            account_number="0",
            start=date(2024, 5, 1),
            end=date(2024, 5, 2),
        )

    assert len(result) == 2
    may1 = next(r for r in result if r["day"] == date(2024, 5, 1))
    may2 = next(r for r in result if r["day"] == date(2024, 5, 2))
    assert may1["kwh_generated"] == pytest.approx(8.0)   # abs(-8) = generation
    assert may1["kwh_consumed"] == pytest.approx(0.0)
    assert may2["kwh_consumed"] == pytest.approx(3.0)    # +3 = net consumption
    assert may2["kwh_generated"] == pytest.approx(0.0)


# ─── is_smarthub_provider ─────────────────────────────────────────────────────

def test_is_smarthub_provider_known():
    for code in ALL_SMARTHUB_PROVIDERS:
        assert is_smarthub_provider(code), f"{code!r} should be a SmartHub provider"
        assert is_smarthub_provider(code.upper()), "case-insensitive"


def test_is_smarthub_provider_unknown():
    assert not is_smarthub_provider("gmp")
    assert not is_smarthub_provider("national_grid")
    assert not is_smarthub_provider("")


# ─── parse_extension_payload ──────────────────────────────────────────────────

def _smarthub_payload(provider="wec", **overrides):
    base = {
        "provider": provider,
        "capturedAt": "2024-06-01T12:00:00Z",
        "user": {"hostname": "washingtonelectric.smarthub.coop", "email": "farm@wec.vt"},
        "auth": {"apiToken": "tok_intercepted"},
        "accounts": [
            {"accountNumber": "8001234", "customerName": "Green Meadow Farm"}
        ],
        "bills": [],
        "usage": [],
    }
    base.update(overrides)
    return base


def test_parse_extension_payload_wec():
    normalized = parse_extension_payload(_smarthub_payload())
    assert normalized["provider"] == "wec"
    assert len(normalized["accounts"]) == 1
    assert normalized["accounts"][0]["account_number"] == "8001234"
    assert normalized["accounts"][0]["nickname"] == "Green Meadow Farm"
    assert normalized["auth"]["apiToken"] == "tok_intercepted"


def test_parse_extension_payload_vec_backward_compat():
    payload = _smarthub_payload(provider="vec")
    payload["user"]["hostname"] = "vermontelectric.smarthub.coop"
    normalized = parse_extension_payload(payload)
    assert normalized["provider"] == "vec"


def test_parse_extension_payload_unknown_code_resolves_via_hostname():
    # v1.6.2: the page hostname is authoritative. An unknown provider code
    # with a curated host resolves to that host's code (was: vec masquerade).
    payload = _smarthub_payload(provider="unknownutility")
    normalized = parse_extension_payload(payload)
    assert normalized["provider"] == "wec"


def test_parse_extension_payload_unknown_host_mints_discovered_code():
    payload = _smarthub_payload(provider="unknownutility")
    payload["user"] = {"hostname": "mysterycoop.smarthub.coop"}
    normalized = parse_extension_payload(payload)
    assert normalized["provider"] == "sh_mysterycoop"
    assert normalized["smarthub_discovered"] is True


def test_parse_extension_payload_no_hostname_falls_back_to_vec():
    # Legacy payloads with no usable hostname keep the old fallback.
    payload = _smarthub_payload(provider="unknownutility")
    payload["user"] = {}
    normalized = parse_extension_payload(payload)
    assert normalized["provider"] == "vec"


def test_parse_extension_payload_uppercase_provider():
    payload = _smarthub_payload(provider="WEC")
    normalized = parse_extension_payload(payload)
    assert normalized["provider"] == "wec"


def test_parse_extension_payload_dedupes_accounts():
    payload = _smarthub_payload(accounts=[
        {"accountNumber": "8001234", "customerName": "A"},
        {"accountNumber": "8001234", "customerName": "A (dup)"},
    ])
    normalized = parse_extension_payload(payload)
    assert len(normalized["accounts"]) == 1


def test_parse_extension_payload_falls_back_to_bills():
    payload = _smarthub_payload(
        provider="wec",
        accounts=[],
        bills=[{"account_id": "9999", "customer_name": "Hill Farm", "service_address": "1 Hill Rd"}],
    )
    normalized = parse_extension_payload(payload)
    assert len(normalized["accounts"]) == 1
    assert normalized["accounts"][0]["account_number"] == "9999"


# ─── parse_usage (parity with vec.py — same implementation via smarthub.py) ───

def test_parse_usage_normal():
    label = (
        "Jun 2023 Billing Period. Usage Dates: May 18 - June 17. "
        "Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"
    )
    row = parse_usage(label)
    assert row is not None
    assert row["meter_id"] == "63698951"
    assert row["kwh"] == 0.0
    assert row["period_start"] == datetime(2023, 5, 18)
    assert row["period_end"] == datetime(2023, 6, 17)


def test_parse_usage_invalid():
    assert parse_usage("not valid") is None
    assert parse_usage("") is None


# ─── parse_bill ───────────────────────────────────────────────────────────────

def test_parse_bill_basic():
    row = {
        "account_id": "8001234",
        "customer_name": "Green Meadow Farm",
        "service_address": "1 Hill Rd",
        "billing_date": "03/15/2024",
        "bill_amount": "-112.34",
        "adjustments": "0.00",
        "total_due": "-112.34",
        "pdf_url": "https://host/bill?uuid=abc",
        "bill_uuid": "abc",
        "bill_timestamp": "1234567890",
    }
    parsed = parse_bill(row)
    assert parsed["account_id"] == "8001234"
    assert parsed["billing_date"] == datetime(2024, 3, 15)
    assert parsed["bill_amount"] == pytest.approx(-112.34)
    assert parsed["bill_uuid"] == "abc"


def test_parse_bill_no_date():
    parsed = parse_bill({"account_id": "123", "billing_date": ""})
    assert parsed["billing_date"] is None
