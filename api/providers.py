"""Supported utility data providers — single source of truth for the UI
dropdown and the per-provider adapter routing.

`code` is what we store in `utility_accounts.provider` (lowercase).
`scrape_status` tells the customer what to expect on onboarding:
  - "live"          : automated Chrome-extension scraping works today
  - "in-progress"   : adapter being built; manual upload until ready
  - "manual"        : we know there's no public API/portal — customer
                      will need to send us PDFs (we OCR them)
"""
from __future__ import annotations
from typing import TypedDict


class ProviderDef(TypedDict):
    code: str
    label: str
    portal_url: str
    state: str
    scrape_status: str
    notes: str


PROVIDERS: list[ProviderDef] = [
    {
        "code": "gmp",
        "label": "Green Mountain Power (GMP)",
        "portal_url": "https://greenmountainpower.com",
        "state": "VT",
        "scrape_status": "live",
        "notes": "85% of VT solar generation. Fully automated via Chrome extension.",
    },
    {
        "code": "vec",
        "label": "Vermont Electric Cooperative",
        "portal_url": "https://vermontelectric.smarthub.coop",
        "state": "VT",
        "scrape_status": "live",
        "notes": "NISC SmartHub portal — Chrome extension scrapes billing history and usage data. kWh data unverified on production generation accounts; test with a real VEC generation meter before trusting reports.",
    },
    {
        "code": "bed",
        "label": "Burlington Electric Department",
        "portal_url": "https://burlingtonelectric.com",
        "state": "VT",
        "scrape_status": "in-progress",
        "notes": "BED MyAccount — adapter coming. Manual PDF upload accepted.",
    },
    {
        "code": "wec",
        "label": "Washington Electric Co-op",
        "portal_url": "https://wec.coop",
        "state": "VT",
        "scrape_status": "in-progress",
        "notes": "Washington EC — adapter coming.",
    },
    {
        "code": "stowe",
        "label": "Stowe Electric Department",
        "portal_url": "https://stoweelectric.com",
        "state": "VT",
        "scrape_status": "in-progress",
        "notes": "Stowe muni — adapter coming.",
    },
    {
        "code": "hardwick",
        "label": "Hardwick Electric Department",
        "portal_url": "https://hardwickelectric.com",
        "state": "VT",
        "scrape_status": "in-progress",
        "notes": "Hardwick muni — adapter coming.",
    },
    {
        "code": "vpps",
        "label": "Village of Stowe / VPPSA member utility",
        "portal_url": "https://vppsa.com",
        "state": "VT",
        "scrape_status": "manual",
        "notes": "Most VPPSA members issue paper bills. Customer emails PDFs; we OCR.",
    },
    {
        "code": "eversource",
        "label": "Eversource (MA / CT / NH)",
        "portal_url": "https://eversource.com",
        "state": "MA",
        "scrape_status": "in-progress",
        "notes": "Eversource MyAccount — adapter coming. NE-wide expansion.",
    },
    {
        "code": "national_grid",
        "label": "National Grid (MA / RI)",
        "portal_url": "https://nationalgridus.com",
        "state": "MA",
        "scrape_status": "in-progress",
        "notes": "National Grid customer portal — adapter coming.",
    },
    {
        "code": "unitil",
        "label": "Unitil / Fitchburg G&E",
        "portal_url": "https://unitil.com",
        "state": "MA",
        "scrape_status": "in-progress",
        "notes": "Unitil customer portal — adapter coming.",
    },
    {
        "code": "ui",
        "label": "United Illuminating (CT)",
        "portal_url": "https://uinet.com",
        "state": "CT",
        "scrape_status": "in-progress",
        "notes": "UI customer portal — adapter coming.",
    },
    {
        "code": "nhec",
        "label": "New Hampshire Electric Cooperative",
        "portal_url": "https://nhec.com",
        "state": "NH",
        "scrape_status": "in-progress",
        "notes": "NHEC SmartHub — adapter coming.",
    },
    {
        "code": "other",
        "label": "Other (manual upload)",
        "portal_url": "",
        "state": "",
        "scrape_status": "manual",
        "notes": "Customer emails PDF bills; we OCR and ingest manually.",
    },
]


PROVIDER_CODES = {p["code"] for p in PROVIDERS}


def get_provider(code: str) -> ProviderDef | None:
    code = (code or "").lower().strip()
    for p in PROVIDERS:
        if p["code"] == code:
            return p
    return None
