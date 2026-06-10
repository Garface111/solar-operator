"""Provider registry integrity — guards the CSV catalog that swarm lanes edit.

These tests make the data-driven registry safe to fan out across many agents:
each lane edits one state CSV, and CI here catches schema drift, duplicate
codes, unwired "live" claims, and registry desync before anything ships.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from api.providers import (  # noqa: E402
    PROVIDERS,
    PROVIDER_CODES,
    SMARTHUB_HOSTS,
    BESPOKE_LIVE_CODES,
    get_provider,
)


def test_all_providers_loaded():
    assert len(PROVIDERS) >= 25
    assert len(PROVIDER_CODES) == len(PROVIDERS), "codes must be unique"


def test_every_provider_resolves():
    for p in PROVIDERS:
        assert get_provider(p["code"]) is not None
        assert get_provider(p["code"].upper()) is not None, "case-insensitive"


def test_status_values_valid():
    for p in PROVIDERS:
        assert p["scrape_status"] in {"live", "in-progress", "manual"}, p


def test_live_means_wired():
    """A 'live' provider must be EITHER a SmartHub host OR a known bespoke
    adapter — never an unbacked claim of automation."""
    for p in PROVIDERS:
        if p["scrape_status"] == "live":
            assert p["smarthub_host"] or p["code"] in BESPOKE_LIVE_CODES, (
                f"{p['code']} is live but neither SmartHub-wired nor a known "
                f"bespoke adapter"
            )


def test_smarthub_hosts_well_formed():
    seen = set()
    for code, host in SMARTHUB_HOSTS.items():
        assert host.endswith(".smarthub.coop"), host
        assert host not in seen, f"duplicate host {host}"
        seen.add(host)
        p = get_provider(code)
        assert p and p["scrape_status"] == "live"


def test_smarthub_adapter_registry_matches_catalog():
    """The universal adapter's registry must derive exactly from the catalog."""
    from api.adapters.smarthub import ALL_SMARTHUB_PROVIDERS, HOST_TO_PROVIDER

    assert set(ALL_SMARTHUB_PROVIDERS) == set(SMARTHUB_HOSTS.keys())
    assert set(HOST_TO_PROVIDER.keys()) == set(SMARTHUB_HOSTS.values())


def test_extension_registry_js_in_sync():
    """The generated extension JS must match the catalog (run the codegen
    --check). Catches a CSV edit that forgot to regenerate the JS."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_smarthub_registry_js.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "extension/smarthub_registry.js is stale — run "
        "`python scripts/gen_smarthub_registry_js.py`.\n" + result.stderr
    )
