"""Supported utility data providers — the UI dropdown + adapter-routing catalog.

SINGLE SOURCE OF TRUTH: the per-state CSV files in `api/data/providers/*.csv`.
This module loads, validates, and merges them at import time. Add or edit a
utility by editing the CSV for its state — NOT by editing Python.

Why CSV-per-state (V1→V2 nationwide build):
  - Each agent swarm lane owns ONE state file → zero merge conflicts by
    construction when fanning out across the country.
  - `GET /v1/providers` reflects new utilities the instant the container
    deploys; no frontend rebuild needed.
  - The SmartHub universal adapter derives its host registry from the same
    rows (the `smarthub_host` column), so a `live` SmartHub utility is wired
    end-to-end from a single CSV line.

CSV columns (header required, order-independent):
  code           lowercase unique id stored in utility_accounts.provider
  label          human-readable name shown in the UI
  state          two-letter region (VT, NH, MA, ...) or empty for multi-state
  scrape_status  live | in-progress | manual   (see below)
  smarthub_host  <utility>.smarthub.coop host — REQUIRED iff this is a live
                 SmartHub utility; empty otherwise
  portal_url     customer login URL (optional)
  notes          free text; flag caveats LOUDLY here

scrape_status:
  - "live"          automated scraping works today (SmartHub host wired, or a
                    bespoke adapter like GMP exists)
  - "in-progress"   adapter being built; manual PDF upload until ready
  - "manual"        no public API/portal — customer emails PDFs we OCR

INVARIANT enforced at load: a row with smarthub_host set MUST be
scrape_status="live", and a "live" row is EITHER a SmartHub host OR one of the
known bespoke-adapter codes (BESPOKE_LIVE_CODES). This prevents a CSV edit from
silently claiming automation that doesn't exist — the one failure mode this
product cannot tolerate (wrong kWh on a customer's NEPOOL report).
"""
from __future__ import annotations

import csv
import pathlib
from typing import TypedDict


class ProviderDef(TypedDict):
    code: str
    label: str
    portal_url: str
    state: str
    scrape_status: str
    smarthub_host: str
    notes: str


_DATA_DIR = pathlib.Path(__file__).resolve().parent / "data" / "providers"

_VALID_STATUS = {"live", "in-progress", "manual"}

# Codes that are "live" via a dedicated bespoke adapter (NOT SmartHub).
# A live row must be either a SmartHub host or one of these.
BESPOKE_LIVE_CODES = {
    "gmp",
    # Eversource Energy (CT / MA / NH) — bespoke MyAccount portal, cloud-capture
    # harvester in api/harvester/vendors/eversource.py (Jul 2026).
    "eversource", "eversource_ma", "eversource_ct",
}

_REQUIRED_COLS = {
    "code", "label", "state", "scrape_status", "smarthub_host", "portal_url", "notes",
}


class ProviderRegistryError(ValueError):
    """Raised when the CSV catalog is malformed — fail fast at import."""


def _norm(row: dict[str, str]) -> ProviderDef:
    return {
        "code": (row.get("code") or "").strip().lower(),
        "label": (row.get("label") or "").strip(),
        "state": (row.get("state") or "").strip().upper(),
        "scrape_status": (row.get("scrape_status") or "").strip().lower(),
        "smarthub_host": (row.get("smarthub_host") or "").strip().lower(),
        "portal_url": (row.get("portal_url") or "").strip(),
        "notes": (row.get("notes") or "").strip(),
    }


def _load_all() -> list[ProviderDef]:
    if not _DATA_DIR.is_dir():
        raise ProviderRegistryError(f"provider data dir missing: {_DATA_DIR}")

    providers: list[ProviderDef] = []
    seen_codes: dict[str, str] = {}          # code -> source file
    seen_hosts: dict[str, str] = {}          # host -> code

    for path in sorted(_DATA_DIR.glob("*.csv")):
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            cols = set(reader.fieldnames or [])
            missing = _REQUIRED_COLS - cols
            if missing:
                raise ProviderRegistryError(
                    f"{path.name}: missing column(s) {sorted(missing)}"
                )
            for lineno, raw in enumerate(reader, start=2):
                p = _norm(raw)
                if not p["code"]:
                    continue  # tolerate blank trailing lines
                where = f"{path.name}:{lineno}"

                if p["code"] in seen_codes:
                    raise ProviderRegistryError(
                        f"{where}: duplicate code {p['code']!r} "
                        f"(first seen {seen_codes[p['code']]})"
                    )
                seen_codes[p["code"]] = where

                if p["scrape_status"] not in _VALID_STATUS:
                    raise ProviderRegistryError(
                        f"{where}: invalid scrape_status {p['scrape_status']!r} "
                        f"(use one of {sorted(_VALID_STATUS)})"
                    )

                host = p["smarthub_host"]
                if host:
                    if not host.endswith(".smarthub.coop"):
                        raise ProviderRegistryError(
                            f"{where}: smarthub_host {host!r} must end with "
                            "'.smarthub.coop'"
                        )
                    if p["scrape_status"] != "live":
                        raise ProviderRegistryError(
                            f"{where}: {p['code']} has a smarthub_host but "
                            f"scrape_status={p['scrape_status']!r} — a wired "
                            "SmartHub utility must be 'live'."
                        )
                    if host in seen_hosts:
                        raise ProviderRegistryError(
                            f"{where}: smarthub_host {host!r} already used by "
                            f"{seen_hosts[host]!r}"
                        )
                    seen_hosts[host] = p["code"]

                if p["scrape_status"] == "live" and not host \
                        and p["code"] not in BESPOKE_LIVE_CODES:
                    raise ProviderRegistryError(
                        f"{where}: {p['code']} is 'live' but has no "
                        "smarthub_host and is not a known bespoke adapter "
                        f"({sorted(BESPOKE_LIVE_CODES)}). Refusing to claim "
                        "automation that isn't wired."
                    )

                providers.append(p)

    if not providers:
        raise ProviderRegistryError(f"no providers loaded from {_DATA_DIR}")

    # Stable ordering: live first, then by state, then code. Keeps the UI
    # dropdown deterministic regardless of file read order.
    _status_rank = {"live": 0, "in-progress": 1, "manual": 2}
    providers.sort(key=lambda p: (
        _status_rank.get(p["scrape_status"], 9),
        p["state"] or "ZZ",
        p["code"],
    ))
    return providers


PROVIDERS: list[ProviderDef] = _load_all()

PROVIDER_CODES = {p["code"] for p in PROVIDERS}

# code -> smarthub host, for the universal SmartHub adapter to consume.
SMARTHUB_HOSTS: dict[str, str] = {
    p["code"]: p["smarthub_host"]
    for p in PROVIDERS
    if p["smarthub_host"]
}


def get_provider(code: str) -> ProviderDef | None:
    code = (code or "").lower().strip()
    for p in PROVIDERS:
        if p["code"] == code:
            return p
    return None
