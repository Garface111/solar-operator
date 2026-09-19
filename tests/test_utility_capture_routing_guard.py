"""Capture routing must not guess a utility's portal or send credentials cross-host."""
import asyncio
from types import SimpleNamespace
import pytest
from api.adapters.smarthub import ALL_SMARTHUB_PROVIDERS, PROVIDER_TO_UTILITY, derive_provider_from_host
from api.harvester.vendors import module_for

@pytest.mark.parametrize("provider", ["", "other", "vpps", "barton", "bed", "unknown", "sh_", "sh_bad/path", "sh_bad@host", "sh_" + "a" * 38])
def test_unsupported_codes_do_not_route_to_smarthub(provider):
    assert module_for(provider) is None

@pytest.mark.parametrize("provider", sorted(ALL_SMARTHUB_PROVIDERS))
def test_catalog_smarthub_routes_only_to_its_own_host(provider):
    vendor = module_for(provider)
    host = PROVIDER_TO_UTILITY[provider]["host"]
    creds = SimpleNamespace(provider=provider, login_host=host.upper())
    assert asyncio.run(vendor.login_url(creds)) == f"https://{host}/"

@pytest.mark.parametrize("host", ["norwich-solar.smarthub.coop", "new.utility.smarthub.coop"])
def test_valid_discovered_host_keeps_dynamic_capture(host):
    provider = derive_provider_from_host(host)["provider"]
    creds = SimpleNamespace(provider=provider, login_host=host)
    assert asyncio.run(module_for(provider).login_url(creds)) == f"https://{host}/"

@pytest.mark.parametrize("host", ["", "evil.example", "vec.smarthub.coop.evil.example", "user@vec.smarthub.coop", "https://vec.smarthub.coop", "vec.smarthub.coop/path", "vec.smarthub.coop:443", "-bad.smarthub.coop", "bad_.smarthub.coop", "other.smarthub.coop"])
def test_bad_or_mismatched_host_rejected_before_navigation(host):
    creds = SimpleNamespace(provider="vec", login_host=host)
    with pytest.raises(RuntimeError):
        asyncio.run(module_for("vec").login_url(creds))

@pytest.mark.parametrize("provider", ["gmp", "eversource", "eversource_ma", "eversource_ct", "cmp", "fronius", "sma", "chint"])
def test_bespoke_routes_preserved(provider):
    assert module_for(provider) is not None

@pytest.mark.parametrize("provider", ["solaredge", "solis", "enphase", "tigo", "alsoenergy", "locus"])
def test_api_only_inverters_remain_skipped(provider):
    assert module_for(provider) is None
