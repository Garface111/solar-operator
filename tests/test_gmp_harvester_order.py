"""GMP cloud harvester must POST /v1/sync BEFORE utility-meter-capture.

Generation reports need Client + arrays created under that client. Meter
capture creates arrays with client_id=None; if it runs first, /v1/sync used to
skip those accounts and leave report clients empty.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def test_gmp_scrape_emits_sync_before_meter_capture():
    from api.harvester.vendors.gmp import GMPVendor

    vendor = GMPVendor()
    store = {
        "user": {
            "apitoken": "jwt-test",
            "apitokenExpires": "2099-01-01T00:00:00Z",
            "refreshtoken": "rt",
            "email": "op@gmp.test",
            "username": "op@gmp.test",
            "fullName": "Op Name",
            "accountId": "acc1",
            "accounts": [{
                "accountNumber": "100",
                "nickname": "Barn",
                "personId": "p1",
                "solarNetMeter": True,
                "isPrimary": True,
            }],
        }
    }
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=json.dumps(store))

    async def fake_get(api, path):
        if path == "/users/current":
            return {"customData": {"energyAccounts": [{
                "accountNumber": "100", "nickname": "Barn",
            }]}}
        if path.startswith("/usage/") and path.endswith("/summary"):
            return {"isNetMetered": True, "totalGrossGenerated": 100}
        return {}

    async def fake_daily(api, acct_no):
        return [{"day": "2026-01-01", "generated_kwh": 12.0}]

    with patch.object(vendor, "_get", side_effect=fake_get), \
         patch.object(vendor, "_daily_backfill", side_effect=fake_daily), \
         patch("api.harvester.vendors.gmp.httpx.AsyncClient") as Client:
        Client.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace())
        Client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = asyncio.run(vendor.scrape(page, None, SimpleNamespace()))

    paths = [r.path for r in result.requests]
    assert "/v1/sync" in paths
    assert "/v1/array-owners/utility-meter-capture" in paths
    assert paths.index("/v1/sync") < paths.index(
        "/v1/array-owners/utility-meter-capture"
    )
