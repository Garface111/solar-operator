"""Chint / CPS inverter source — extension readings-capture (no backend key path).

As of June 2026 recon there is NO public API documentation for the Chint/CPS
cloud (solar.chintpower.com is a Fomware-built white-label), and no owner-facing
API key to paste. So Chint connects the SAME way as Fronius and SMA: the
EnergyAgent browser extension reads the owner's live readings from the portal
they're already logged into and POSTs them to /v1/array-owners/inverter-capture
("chint" is in that route's _CAPTURE_VENDORS allowlist). This module therefore
has no key-based pull — validate()/fetch_live()/fetch_daily() stay stubs, and
the connect UI offers the one-click "Log in with Chint" path instead of fields.

CAVEAT: unlike SolarEdge/Fronius/SMA, the extension's Chint extraction endpoints
are NOT yet grounded against a live account (no CHINT login was available to
HAR-capture). The content script (extension/chint_content.js) fails gracefully
until they're verified. Grounding that contract is the remaining work.
"""
from __future__ import annotations

from datetime import date

from .base import InverterError

CODE = "chint"
LABEL = "Chint / CPS"
# No backend key-paste path; the extension ingests readings directly. The connect
# UI surfaces Chint via the one-click portal-login flow (LOGIN_VENDORS), not the
# manual key grid, so AVAILABLE stays False for the key-based catalog.
AVAILABLE = False
NOTE = (
    "Chint/CPS has no public API and no key to paste — connect with one click via "
    "the EnergyAgent extension (log into your Chint Power Monitor portal at "
    "monitor.chintpowersystems.com). Extension capture is grounded against live "
    "accounts as of June 2026."
)
SUPPORTS_LIVE = False
SUPPORTS_DAILY = False
FIELDS: list[dict] = []


def validate(config: dict) -> dict:
    raise InverterError(NOTE)


def fetch_live(config: dict) -> dict | None:
    return None


def fetch_daily(config: dict, start: date, end: date) -> list[dict]:
    return []
