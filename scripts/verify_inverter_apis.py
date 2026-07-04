"""Live verification harness for the cloud inverter-API adapters.

The Fronius (api/inverters/fronius.py) and SMA (api/inverters/sma.py) adapters
were written against the vendors' PUBLISHED docs but carry loud "unverified
against a live account" banners. This script removes that caveat: it drives the
REAL adapter code (validate → fetch_live → fetch_daily) against live endpoints
and prints exactly what parsed, so a green run means the adapter is
production-ready as-is.

RUN THIS FROM A MACHINE WITH NORMAL INTERNET (Ford's laptop) — the Claude
sandbox's network policy blocks the vendor hosts, which is why this exists as
a hand-off script instead of having been run in-session.

    cd ~/solar-operator && source venv/bin/activate
    python -m scripts.verify_inverter_apis                # both vendors
    python -m scripts.verify_inverter_apis --vendor fronius

Fronius — works OUT OF THE BOX: defaults to the demo credentials Fronius
publishes for its public demo PV system (the same ones shipped in their API
docs and the fronius_solarweb client). Override with env vars to test a real
system:
    FRONIUS_ACCESS_KEY_ID / FRONIUS_ACCESS_KEY_VALUE / FRONIUS_PV_SYSTEM_ID

SMA — needs credentials first (there is no open sandbox): email SMA API
Developer Support via https://developer.sma.de/sma-sandbox-apis for sandbox
client credentials, then:
    SMA_CLIENT_ID / SMA_CLIENT_SECRET / SMA_SYSTEM_ID  (required)
    SMA_REFRESH_TOKEN                                   (optional)
    SMA_SANDBOX=1     — point the adapter at sandbox.smaapis.de. The sandbox
                        URL layout below follows SMA's docs but is best-effort
                        until the first real sandbox run; production URLs are
                        the adapter's defaults and need no override.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

# Run as `python -m scripts.verify_inverter_apis` from the repo root.
from api.inverters import fronius, sma
from api.inverters.base import InverterAuthError, InverterError

# Fronius's PUBLIC demo system — published by Fronius in its own API docs for
# exactly this kind of evaluation. Not a secret.
FRONIUS_DEMO = {
    "access_key_id": "FKIAFEF58CFEFA94486F9C804CF6077A01AB",
    "access_key_value": "47c076bc-23e5-4949-37a6-4bcfcf8d21d6",
    "pv_system_id": "20bb600e-019b-4e03-9df3-a0a900cda689",
}

GREEN, RED, YELLOW, END = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def _ok(msg: str) -> None:
    print(f"  {GREEN}✓{END} {msg}")


def _fail(msg: str) -> None:
    print(f"  {RED}✗{END} {msg}")


def _warn(msg: str) -> None:
    print(f"  {YELLOW}–{END} {msg}")


def _drive(name: str, mod, config: dict) -> bool:
    """validate → fetch_live → fetch_daily(7d) through the real adapter."""
    print(f"\n== {name} ==")
    passed = True
    try:
        info = mod.validate(config)
        _ok(f"validate: {info!r}")
    except InverterAuthError as e:
        _fail(f"validate AUTH error: {e}")
        return False
    except InverterError as e:
        _fail(f"validate error: {e}")
        return False

    try:
        live = mod.fetch_live(config)
        if live is None:
            _warn("fetch_live: no live value (can be normal at night) — shape parsed OK")
        else:
            _ok(f"fetch_live: {live!r}")
    except InverterError as e:
        _fail(f"fetch_live error: {e}")
        passed = False

    try:
        end = date.today()
        days = mod.fetch_daily(config, end - timedelta(days=7), end)
        if days:
            _ok(f"fetch_daily: {len(days)} day(s); first={days[0]!r} last={days[-1]!r}")
        else:
            _warn("fetch_daily: parsed OK but returned 0 days — check the demo "
                  "system has recent production, or widen the range")
    except InverterError as e:
        _fail(f"fetch_daily error: {e}")
        passed = False
    return passed


def _fronius_config() -> dict:
    return {
        "access_key_id": os.environ.get("FRONIUS_ACCESS_KEY_ID", FRONIUS_DEMO["access_key_id"]),
        "access_key_value": os.environ.get("FRONIUS_ACCESS_KEY_VALUE", FRONIUS_DEMO["access_key_value"]),
        "pv_system_id": os.environ.get("FRONIUS_PV_SYSTEM_ID", FRONIUS_DEMO["pv_system_id"]),
    }


def _sma_config() -> dict | None:
    cid = os.environ.get("SMA_CLIENT_ID")
    sec = os.environ.get("SMA_CLIENT_SECRET")
    sysid = os.environ.get("SMA_SYSTEM_ID")
    if not (cid and sec and sysid):
        return None
    cfg = {"client_id": cid, "client_secret": sec, "system_id": sysid}
    if os.environ.get("SMA_REFRESH_TOKEN"):
        cfg["refresh_token"] = os.environ["SMA_REFRESH_TOKEN"]
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vendor", choices=["fronius", "sma", "all"], default="all")
    args = ap.parse_args()

    results: dict[str, bool] = {}

    if args.vendor in ("fronius", "all"):
        cfg = _fronius_config()
        using_demo = cfg["pv_system_id"] == FRONIUS_DEMO["pv_system_id"]
        print(f"Fronius target: {'PUBLIC DEMO system' if using_demo else cfg['pv_system_id']}")
        results["fronius"] = _drive("Fronius (Solar.web Query API)", fronius, cfg)

    if args.vendor in ("sma", "all"):
        cfg = _sma_config()
        if cfg is None:
            _warn("SMA skipped: set SMA_CLIENT_ID / SMA_CLIENT_SECRET / SMA_SYSTEM_ID "
                  "(get sandbox credentials from SMA API Developer Support — "
                  "https://developer.sma.de/sma-sandbox-apis)")
        else:
            if os.environ.get("SMA_SANDBOX"):
                # Best-effort sandbox layout per SMA's docs; adjust here if the
                # first sandbox run 404s and note the real paths in sma.py.
                sma.AUTH_URL = os.environ.get(
                    "SMA_AUTH_URL", "https://sandbox.smaapis.de/oauth2/token")
                sma.MON_BASE = os.environ.get(
                    "SMA_MON_BASE", "https://sandbox.smaapis.de/monitoring/v1")
                print(f"SMA target: SANDBOX ({sma.MON_BASE})")
            else:
                print("SMA target: PRODUCTION")
            results["sma"] = _drive("SMA (Monitoring API)", sma, cfg)

    print()
    if not results:
        print("Nothing verified — check the flags/env above.")
        return 1
    for vendor, ok in results.items():
        print(f"{vendor}: {'PASS — adapter verified against live API' if ok else 'FAIL — see errors above'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
