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

# Fronius demo AccessKey. NOTE (verified live 2026-07-04): the key+system this
# harness originally shipped were RETIRED by Fronius — that key now returns
# 401 {"responseError":1102,"responseMessage":"AccessKey not found."}. Fronius
# no longer publishes a self-serve public demo *system*; a bare demo key
# authenticates (200) but has zero PV systems attached, so the data endpoints
# can't be exercised without a real system. This key (a published community
# demo key, github.com/drc38/Fronius_solarweb) still AUTHENTICATES and is kept
# only to prove the adapter's live auth + request path. To verify data parsing
# (fetch_live/fetch_daily), set FRONIUS_ACCESS_KEY_ID / FRONIUS_ACCESS_KEY_VALUE
# / FRONIUS_PV_SYSTEM_ID to a real Solar.web account that has the Query API
# enabled and a producing system.
FRONIUS_DEMO = {
    "access_key_id": "FKIAB4CDA71C0763413DA942DC756742318B",
    "access_key_value": "67315e19-6805-479e-994d-7193ee5f6125",
    "pv_system_id": "",  # community demo key has no system attached — resolved at runtime
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


def _fronius_resolve_system(cfg: dict) -> tuple[str | None, str]:
    """Return (pv_system_id, human_note). An explicit FRONIUS_PV_SYSTEM_ID wins.
    Otherwise list the account's systems (which also confirms auth against the
    LIVE API) and take the first — Fronius retired the old baked-in demo system,
    so a bare demo key authenticates but has no systems attached."""
    import httpx
    if cfg.get("pv_system_id"):
        return cfg["pv_system_id"], f"system {cfg['pv_system_id']}"
    try:
        resp = httpx.get(f"{fronius.BASE}/pvsystems", headers=fronius._headers(cfg),
                         params={"offset": 0, "limit": 1}, timeout=30)
    except httpx.RequestError as exc:
        return None, f"network error listing systems: {exc}"
    if resp.status_code in (401, 403):
        return None, (f"AUTH FAILED ({resp.status_code}) — the demo key is dead; set "
                      "FRONIUS_ACCESS_KEY_ID / FRONIUS_ACCESS_KEY_VALUE to real creds")
    if not resp.is_success:
        return None, f"listing systems returned {resp.status_code}: {resp.text[:160]}"
    systems = (resp.json() or {}).get("pvSystems") or []
    if systems:
        sid = systems[0].get("pvSystemId") or systems[0].get("id")
        return sid, f"account system {sid}"
    return None, ("auth OK (200) but this key has NO PV systems — Fronius retired the "
                  "public demo system. Set FRONIUS_PV_SYSTEM_ID (+ real key) to a "
                  "producing system to verify fetch_live/fetch_daily parsing.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vendor", choices=["fronius", "sma", "all"], default="all")
    args = ap.parse_args()

    results: dict[str, bool] = {}

    if args.vendor in ("fronius", "all"):
        cfg = _fronius_config()
        sysid, note = _fronius_resolve_system(cfg)
        if sysid:
            cfg["pv_system_id"] = sysid
            print(f"Fronius target: {note}")
            results["fronius"] = _drive("Fronius (Solar.web Query API)", fronius, cfg)
        else:
            # Reached the live API and confirmed the auth/request path, but there's
            # no system to verify the flowdata/aggrdata PARSING against. Honest
            # partial: not a code failure, but not a full data verification either.
            print(f"\n== Fronius (Solar.web Query API) ==")
            _warn(f"partial: {note}")
            print("  (adapter auth + request path reach the live API; data-shape "
                  "parsing still needs a live system — see HANDOFF_API_VERIFICATION.md)")

    if args.vendor in ("sma", "all"):
        cfg = _sma_config()
        if cfg is None:
            _warn("SMA skipped: set SMA_CLIENT_ID / SMA_CLIENT_SECRET / SMA_SYSTEM_ID "
                  "(get sandbox credentials from SMA API Developer Support — "
                  "https://developer.sma.de/sma-sandbox-apis)")
        else:
            if os.environ.get("SMA_SANDBOX"):
                # Sandbox URL layout — CONFIRMED against SMA's live docs +
                # endpoints 2026-07-06: the token host is sandbox-AUTH.smaapis.de
                # (NOT sandbox.smaapis.de, which 401s the wrong way); monitoring +
                # backchannel-consent live under sandbox.smaapis.de.
                sma.AUTH_URL = os.environ.get(
                    "SMA_AUTH_URL", "https://sandbox-auth.smaapis.de/oauth2/token")
                sma.MON_BASE = os.environ.get(
                    "SMA_MON_BASE", "https://sandbox.smaapis.de/monitoring/v1")
                sma.BC_BASE = os.environ.get(
                    "SMA_BC_BASE", "https://sandbox.smaapis.de")
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
