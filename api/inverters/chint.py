"""Chint / CPS inverter source — explicit honest stub.

As of June 2026 recon there is NO public API documentation for the Chint/CPS
cloud. Rather than fabricate an integration, this vendor registers in VENDORS
but advertises itself as unavailable: validate() raises a clear InverterError
pointing the operator at the manual-CSV path, fetch_live() returns None and
fetch_daily() returns []. The connect UI renders Chint with a "manual data"
badge (available=False + NOTE) so the funnel stays honest — the operator can
select Chint and get real guidance instead of a silent dead end.

Tracking: Chint's FlexOM gateway is the most likely route to direct support;
revisit when/if they publish an API.
"""
from __future__ import annotations

from datetime import date

from .base import InverterError

CODE = "chint"
LABEL = "Chint / CPS"
AVAILABLE = False
NOTE = (
    "Chint/CPS cloud has no public API — connect via manual CSV upload for now; "
    "we're tracking their FlexOM gateway for direct support."
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
