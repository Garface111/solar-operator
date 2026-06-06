"""
DEPRECATED — use api/adapters/smarthub.py. Will be removed Aug 2026.

This module remains for in-flight VEC sessions during the cutover window.
parse_usage / parse_bill / parse_extension_payload are re-exported from
smarthub.py so direct importers continue to work without changes.

Original docstring preserved below for history:

Provider adapter for Vermont Electric Cooperative (NISC SmartHub).

VEC's portal at https://vermontelectric.smarthub.coop uses cookie-based sessions
with CSRF-protected JSON APIs. Server-side pulls are not feasible without the
user's session cookies. Data is instead scraped by the Chrome extension from
the DOM of two pages:

  - /ui/billing/history  — Angular table with bill rows (date, amount, PDF link)
  - /ui/#/usageExplorer  — SVG chart; kWh exposed as aria-labels on <image> elements

The extension POSTs the scraped data to /v1/sync (provider="vec"). This adapter
normalizes that payload and provides parse helpers. Server-side fetch functions
are intentionally absent — worker.py will skip VEC accounts on the JSON/PDF paths.

SmartHub platform note: NISC SmartHub is used by multiple VT coops
(vermontelectric, washingtonelectric, stoweelectric). The same parsing logic
should work for all of them — only the subdomain differs.

CAVEAT: The test account (6578300 at vermontelectric.smarthub.coop) shows 0 kWh
every month — it is a generation-only credit account. The aria-label scraping
path is structurally correct but MUST be verified against a real generation-meter
account before trusting production data.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

PROVIDER = "vec"

# ─── aria-label parsing ───────────────────────────────────────────────────────
# Format: "Jun 2023 Billing Period. Usage Dates: May 18 - June 17.
#          Meter 63698951 - Consumption - kWh: 0 kWh. Average Temperature: 58 °F"

_ARIA_RE = re.compile(
    r"^([^\n.]+?)\s+Billing Period\.\s+Usage Dates:\s+([^\n.]+?)\s*\."
    r"[\s\S]+?Meter\s+(\d+)\s+-\s+[^\n\-]+?\s+-\s+kWh:\s+([\d.]+)\s+kWh"
    r"(?:[\s\S]*?Average Temperature:\s+([\d.]+)\s*°?F)?",
    re.IGNORECASE,
)

_USAGE_DATES_RE = re.compile(r"([A-Za-z]+\s+\d{1,2})\s*-\s*([A-Za-z]+\s+\d{1,2})")
_YEAR_RE = re.compile(r"\b(\d{4})\b")
_BILL_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _parse_month_day(s: str, year: int) -> datetime | None:
    """Parse "Month DD" or "Mon DD" strings with the given year."""
    for fmt in ("%B %d", "%b %d"):
        try:
            return datetime.strptime(f"{s.strip()} {year}", f"{fmt} %Y")
        except ValueError:
            continue
    return None


def _parse_usage_dates(
    usage_dates_raw: str, billing_year: int
) -> tuple[datetime | None, datetime | None]:
    dm = _USAGE_DATES_RE.search(usage_dates_raw)
    if not dm:
        return None, None

    end_str = dm.group(2).strip()
    period_end = _parse_month_day(end_str, billing_year)
    if period_end is None:
        return None, None

    start_str = dm.group(1).strip()
    period_start = _parse_month_day(start_str, billing_year)
    if period_start is None:
        return None, period_end

    # Usage period crossing a year boundary (e.g. Jan billing, Dec→Jan dates)
    if period_start > period_end:
        period_start = _parse_month_day(start_str, billing_year - 1)

    return period_start, period_end


def parse_usage(aria_label: str) -> dict[str, Any] | None:
    """Parse one usage-explorer SVG aria-label into a structured usage row.

    Returns None if the label does not match the expected NISC SmartHub format.
    """
    m = _ARIA_RE.match(aria_label.strip())
    if not m:
        return None

    period_label = m.group(1).strip()
    usage_dates_raw = m.group(2).strip()
    meter_id = m.group(3)
    kwh = float(m.group(4))
    avg_temp_f = float(m.group(5)) if m.group(5) else None

    year_m = _YEAR_RE.search(period_label)
    billing_year = int(year_m.group(1)) if year_m else None

    period_start = period_end = None
    if billing_year:
        period_start, period_end = _parse_usage_dates(usage_dates_raw, billing_year)

    return {
        "period_label": period_label,
        "usage_dates_raw": usage_dates_raw,
        "meter_id": meter_id,
        "kwh": kwh,
        "avg_temp_f": avg_temp_f,
        "period_start": period_start,
        "period_end": period_end,
    }


# ─── billing history row parsing ──────────────────────────────────────────────

def _parse_amount(s: str | None) -> float | None:
    if not s:
        return None
    cleaned = s.strip().replace(",", "").replace("$", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_bill(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one billing-history row (from the extension scrape) into a bill dict."""
    billing_date: datetime | None = None
    date_str = row.get("billing_date", "")
    dm = _BILL_DATE_RE.match(date_str.strip())
    if dm:
        billing_date = datetime(int(dm.group(3)), int(dm.group(1)), int(dm.group(2)))

    return {
        "account_id": row.get("account_id"),
        "customer_name": row.get("customer_name"),
        "service_address": row.get("service_address"),
        "billing_date": billing_date,
        "bill_amount": _parse_amount(row.get("bill_amount")),
        "adjustments": _parse_amount(row.get("adjustments")),
        "total_due": _parse_amount(row.get("total_due")),
        "pdf_url": row.get("pdf_url"),
        "bill_uuid": row.get("bill_uuid"),
        "bill_timestamp": row.get("bill_timestamp"),
    }


# ─── extension payload normalization ─────────────────────────────────────────

def parse_extension_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the Chrome extension VEC POST body into the standard shape
    expected by /v1/sync (provider, auth, user, accounts).

    VEC uses cookie-based auth — there is no capturable JWT. The auth block
    is always empty. Bills and usage rows from the scrape are passed through
    as bills_raw / usage_raw for future processing but are not stored in this
    version (server-side bill storage for extension-scraped data is a separate task).
    """
    raw_accounts = payload.get("accounts", [])
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for a in raw_accounts:
        acct_no = (a.get("accountNumber") or a.get("account_id") or "").strip()
        if not acct_no or acct_no in seen:
            continue
        seen.add(acct_no)
        svc_addr = a.get("serviceAddress")
        customer_name = a.get("customerName") or a.get("customer_name")
        accounts.append(
            {
                "account_number": acct_no,
                "customer_number": None,
                "customer_name": customer_name,
                "nickname": customer_name,
                "service_address": (
                    {"line1": svc_addr} if isinstance(svc_addr, str) else svc_addr
                ),
                "extra": {"provider": PROVIDER, "customerName": customer_name},
            }
        )

    # Derive accounts from bill rows when the accounts list is empty
    if not accounts:
        for b in payload.get("bills", []):
            acct_no = (b.get("account_id") or "").strip()
            if not acct_no or acct_no in seen:
                continue
            seen.add(acct_no)
            customer_name = b.get("customer_name")
            accounts.append(
                {
                    "account_number": acct_no,
                    "customer_number": None,
                    "customer_name": customer_name,
                    "nickname": customer_name,
                    "service_address": (
                        {"line1": b["service_address"]}
                        if b.get("service_address")
                        else None
                    ),
                    "extra": {"provider": PROVIDER, "customerName": customer_name},
                }
            )

    return {
        "provider": PROVIDER,
        "captured_at": payload.get("capturedAt"),
        "user": payload.get("user", {}),
        # VEC has no capturable token — cookie auth only
        "auth": {},
        "accounts": accounts,
        # Scraped data from the extension (available for future bill-storage pass)
        "bills_raw": payload.get("bills", []),
        "usage_raw": payload.get("usage", []),
    }


# ── Forwarding re-exports for callers that import directly from this module ───
# The canonical implementations now live in smarthub.py. These aliases keep
# existing imports (e.g. app.py's from .adapters.vec import parse_bill) working
# without modification until the Aug 2026 removal.
from .smarthub import (  # noqa: F401,F811,E402
    parse_usage,
    parse_bill,
    parse_extension_payload,
)
