"""Vendor module interface.

A vendor module knows how to (a) reach its portal's login page, (b) tell whether
a page is already an authenticated session (so a warm persisted session skips
login), and (c) scrape the data and emit it as CaptureRequests — the SAME HTTP
calls the extension already makes to the Array Operator backend. Emitting the
proven request shape (rather than writing the DB directly) means the harvester
feeds data through the identical, battle-tested ingest path: same dup-safety,
same plausibility guards, no second write path to drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CaptureRequest:
    """One capture POST to the Array Operator API, identical to what the
    extension sends. The engine attaches the tenant auth header and fires it."""
    path: str                       # e.g. "/v1/array-owners/inverter-capture"
    body: dict                      # JSON payload
    method: str = "POST"
    note: str = ""                  # human label for the audit trail


@dataclass
class ScrapeResult:
    requests: list[CaptureRequest] = field(default_factory=list)
    # Free-text summary for the HarvestRun audit row (e.g. "3 arrays, 41 days").
    summary: str = ""


class VendorModule(Protocol):
    #: provider codes this module handles (e.g. ["gmp"] or ["*smarthub*"]).
    provider: str

    async def login_url(self, creds) -> str:
        """The URL to navigate to for login (or the post-login landing to test a
        warm session)."""
        ...

    async def is_logged_in(self, page) -> bool:
        """True if `page` is an authenticated session (no login needed)."""
        ...

    async def scrape(self, page, context, creds) -> ScrapeResult:
        """Extract data from an authenticated session and return CaptureRequests."""
        ...
