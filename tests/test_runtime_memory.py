"""Runtime cleanup preserves capture payloads and outage fallback."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.runtime_cache import BoundedTimestampCache
from api.harvester import engine
from api.harvester.response import disposing_response
from api.harvester.vendors.base import CaptureRequest, ScrapeResult


def test_cache_expiration_eviction_and_recent_reads():
    cache = BoundedTimestampCache(max_entries=2, retention=timedelta(minutes=5))
    current = datetime.utcnow()
    cache["expired"] = (current - timedelta(hours=1), "private-old")
    cache["a"] = (current, 1)
    assert "expired" not in cache
    cache["b"] = (current, 2)
    assert cache["a"][1] == 1
    cache["c"] = (current, 3)
    assert set(cache) == {"a", "c"}
    assert len(cache) == 2


def test_cache_threaded_growth_is_bounded():
    from concurrent.futures import ThreadPoolExecutor
    cache = BoundedTimestampCache(max_entries=32, retention=timedelta(minutes=5))
    def write(i):
        cache[i] = (datetime.utcnow(), i)
        cache.get(i)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(1000)))
    assert len(cache) == 32


def test_dashboard_keys_hide_credentials_and_caches_bound():
    from api import array_owners as ao, inverter_fleet as fleet
    secret = "private-api-key-123"
    key = ao._cache_key("vendor", {"api_key": secret, "site": 1})
    assert secret not in key
    assert key == ao._cache_key("vendor", {"site": 1, "api_key": secret})
    assert key != ao._cache_key("vendor", {"site": 1, "api_key": "rotated"})
    for cache in (ao._overview_cache, ao._tree_cache, fleet._site_cache):
        assert cache.max_entries == 512


def test_stale_telemetry_remains_available_during_vendor_outage(monkeypatch):
    from api import inverter_fleet as fleet
    from api.adapters import solaredge
    cache = BoundedTimestampCache(max_entries=512, retention=timedelta(days=7))
    monkeypatch.setattr(fleet, "_site_cache", cache)
    value = {"serial": {"daily": [1], "last_report": "old"}}
    cache["solaredge:42"] = (datetime.utcnow() - timedelta(hours=1), value)
    monkeypatch.setattr(solaredge, "fetch_inventory", Mock(side_effect=solaredge.SolarEdgeError("offline")))
    assert fleet._telemetry_for_site("solaredge", {"api_key": "key"}, 42) == value


@pytest.mark.parametrize("failure", [None, "delivery", "scrape", "close_once"])
def test_context_released_before_delivery_preserving_session(monkeypatch, failure):
    events = []
    state = {"cookies": [{"name": "warm", "value": "session"}]}
    page = SimpleNamespace(
        bring_to_front=AsyncMock(), goto=AsyncMock(), wait_for_load_state=AsyncMock())
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), set_default_timeout=Mock(),
        set_default_navigation_timeout=Mock())
    async def storage_state():
        events.append("state")
        return state
    attempts = 0
    async def close():
        nonlocal attempts
        attempts += 1
        events.append("close")
        if failure == "close_once" and attempts == 1:
            raise RuntimeError("cleanup")
    context.storage_state = storage_state
    context.close = close
    payload = [CaptureRequest(path="/capture", body={"evidence": [1, 2]})]
    async def scrape(*args):
        events.append("scrape")
        if failure == "scrape":
            raise RuntimeError("scrape failure")
        return ScrapeResult(requests=payload, summary="kept")
    vendor = SimpleNamespace(login_url=AsyncMock(return_value="https://invalid.test"),
                             is_logged_in=AsyncMock(return_value=True), scrape=scrape)
    farm = engine.BrowserFarm()
    farm._browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    monkeypatch.setattr(farm, "_load", lambda *a: SimpleNamespace(
        password="pw", username="user", session_state={}))
    persisted = []
    monkeypatch.setattr(farm, "_persist", lambda *a, **kw: persisted.append(kw))
    monkeypatch.setattr(engine, "module_for", lambda p: vendor)
    monkeypatch.setattr(engine.stealth, "apply", AsyncMock())
    monkeypatch.setattr(engine.asyncio, "sleep", AsyncMock())
    async def deliver(tenant, requests):
        events.append("deliver")
        assert events.index("state") < events.index("close") < events.index("deliver")
        assert requests is payload
        if failure == "delivery":
            raise RuntimeError("delivery failure")
        return 2
    monkeypatch.setattr(engine, "deliver", deliver)
    result = asyncio.run(farm._harvest_inner("tenant", "provider", "user"))
    assert persisted[-1]["storage_state"] == state
    assert attempts == (2 if failure == "close_once" else 1)
    assert result.status == ("scrape_failed" if failure in ("delivery", "scrape") else "ok")
    if failure == "scrape":
        assert "deliver" not in events
    if failure == "delivery":
        assert events.count("state") == 1


@pytest.mark.parametrize("failure", [None, "parse", "dispose"])
def test_response_disposed_without_losing_data_or_masking_parse(failure):
    response = SimpleNamespace(dispose=AsyncMock(
        side_effect=RuntimeError("cleanup") if failure == "dispose" else None))
    async def run():
        async with disposing_response(response):
            if failure == "parse":
                raise ValueError("bad JSON")
            return {"retained": "evidence"}
    if failure == "parse":
        with pytest.raises(ValueError, match="bad JSON"):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == {"retained": "evidence"}
    response.dispose.assert_awaited_once()


def test_smarthub_json_and_pdf_release_raw_response_buffers():
    from api.harvester.vendors.smarthub import SmartHubVendor
    raw = b"x" * 1024
    response = SimpleNamespace(ok=True, json=AsyncMock(return_value=[{"id": 1}]),
                               body=AsyncMock(return_value=raw), dispose=AsyncMock())
    req = SimpleNamespace(get=AsyncMock(return_value=response))
    vendor = SmartHubVendor()
    assert asyncio.run(vendor._accounts(req, "https://invalid.test", "user")) == [{"id": 1}]
    response.dispose.assert_awaited_once()
    response.dispose.reset_mock()
    import base64
    encoded = asyncio.run(vendor._pdf(req, "https://invalid.test", {
        "bill_uuid": "id", "account_id": "acct", "billing_date": "01/01/2026"}))
    assert base64.b64decode(encoded) == raw
    response.dispose.assert_awaited_once()


@pytest.mark.parametrize("ok,parse_fails", [(False, False), (True, True)])
def test_smarthub_error_responses_still_dispose(ok, parse_fails):
    from api.harvester.vendors.smarthub import SmartHubVendor
    response = SimpleNamespace(ok=ok, json=AsyncMock(side_effect=ValueError("invalid")),
                               dispose=AsyncMock())
    req = SimpleNamespace(get=AsyncMock(return_value=response))
    assert asyncio.run(SmartHubVendor._overview(req, "https://invalid.test", "acct")) == []
    response.dispose.assert_awaited_once()
    assert response.json.await_count == int(parse_fails)


def test_sma_expired_token_disposes_response_and_preserves_failure():
    from api.harvester.vendors.sma import SMAVendor
    response = SimpleNamespace(status=401, ok=False, dispose=AsyncMock())
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    page = SimpleNamespace(evaluate=AsyncMock(return_value="access-token"))
    with pytest.raises(RuntimeError, match="token expired"):
        asyncio.run(SMAVendor().scrape(page, SimpleNamespace(request=request), None))
    response.dispose.assert_awaited_once()
