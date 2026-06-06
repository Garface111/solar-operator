"""
SmartHub universal adapter.

Works across any NISC SmartHub deployment (~500+ US co-ops/munis). Replaces
the per-utility SVG-scraping approach. Uses the undocumented-but-stable JSON
API the SmartHub SPA itself calls.

Auth flow:
  1. POST /services/oauth/auth/v2 with email + password → authorizationToken + primaryUsername
  2. (One-time per account) GET /services/secured/user-data
     to discover the serviceLocationNumber for each meter
  3. POST /services/secured/utility-usage/poll with date range to pull
     daily / hourly kWh including return-to-grid for net-metering customers

Tested deployments (Jun 2026):
  - vermontelectric.smarthub.coop (VEC)
  - washingtonelectric.smarthub.coop (WEC)
  Add new utilities by config in SMARTHUB_UTILITIES below.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

# Registry of supported utilities. Adding a utility = one entry here.
# provider: lowercase code used in DB (UtilityAccount.provider) and payloads.
SMARTHUB_UTILITIES: dict[str, dict[str, str]] = {
    "VEC": {
        "host": "vermontelectric.smarthub.coop",
        "name": "Vermont Electric Cooperative",
        "provider": "vec",
    },
    "WEC": {
        "host": "washingtonelectric.smarthub.coop",
        "name": "Washington Electric Cooperative",
        "provider": "wec",
    },
    "STOWE": {
        "host": "stoweelectric.smarthub.coop",
        "name": "Stowe Electric Department",
        "provider": "stowe",
    },
    "HYDE_PARK": {
        "host": "villageofhydepark.smarthub.coop",
        "name": "Village of Hyde Park",
        "provider": "hyde_park",
    },
    "LUDLOW": {
        "host": "ludlow.smarthub.coop",
        "name": "Village of Ludlow Electric",
        "provider": "ludlow",
    },
    "ENOSBURG": {
        "host": "villageofenosburgfalls.smarthub.coop",
        "name": "Village of Enosburg Falls",
        "provider": "enosburg",
    },
    "NHEC": {
        "host": "nhec.smarthub.coop",
        "name": "New Hampshire Electric Cooperative",
        "provider": "nhec",
    },
}

# Maps *.smarthub.coop hostname → lowercase provider code
HOST_TO_PROVIDER: dict[str, str] = {
    v["host"]: v["provider"] for v in SMARTHUB_UTILITIES.values()
}
# Maps lowercase provider code → utility config dict
PROVIDER_TO_UTILITY: dict[str, dict[str, str]] = {
    v["provider"]: v for v in SMARTHUB_UTILITIES.values()
}
# All known SmartHub provider codes (lowercase)
ALL_SMARTHUB_PROVIDERS: frozenset[str] = frozenset(HOST_TO_PROVIDER.values())

# SmartHub session timeout matches HA integration constant (300 seconds)
_SESSION_TIMEOUT_SECONDS = 300

# Fallback electric service keys if auto-detection from serviceToServiceDescription fails.
# UNVERIFIED: exact keys vary by deployment; "ELEC" covers most VT co-ops.
_ELECTRIC_KEY_FALLBACKS = ("ELEC", "1ELEC", "VELEC", "GELEC")


def is_smarthub_provider(provider: str) -> bool:
    """Return True if the provider code corresponds to a SmartHub utility."""
    return provider.strip().lower() in ALL_SMARTHUB_PROVIDERS


def _base_url(host: str) -> str:
    return f"https://{host}"


def _auth_headers(email: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Nisc-Smarthub-Username": email,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def authenticate(host: str, email: str, password: str) -> dict[str, Any]:
    """Authenticate with a SmartHub deployment and return a session dict.

    Returns:
        {
            "auth_token": str,           # authorizationToken for bearer auth
            "primary_username": str,     # canonical username (may differ from email)
            "email": str,               # login email as supplied
            "expires_at": datetime,     # UTC datetime when session should be refreshed
        }

    Raises:
        httpx.HTTPStatusError: on HTTP errors (401 = bad credentials)
        ValueError: if the response is missing the authorizationToken field
    """
    url = f"{_base_url(host)}/services/oauth/auth/v2"
    resp = httpx.post(
        url,
        data={"userId": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=True,
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data.get("authorizationToken") or data.get("authorization_token")
    if not token:
        raise ValueError(
            f"SmartHub auth response missing authorizationToken for host={host}"
        )
    # UNVERIFIED: field name for primary username — confirmed from HA integration but
    # some deployments may vary the casing.
    primary_username = (
        data.get("primaryUsername")
        or data.get("primary_username")
        or email
    )

    return {
        "auth_token": token,
        "primary_username": primary_username,
        "email": email,
        "expires_at": datetime.utcnow() + timedelta(seconds=_SESSION_TIMEOUT_SECONDS),
    }


def _detect_electric_service_key(user_data: dict[str, Any]) -> str:
    """Find the electric service key from a user-data response.

    Most SmartHub deployments use "ELEC"; some use "1ELEC", "VELEC", etc.
    Scans serviceToServiceDescription for a key whose value contains "electric".
    """
    service_to_desc: dict = user_data.get("serviceToServiceDescription") or {}
    for key, desc in service_to_desc.items():
        if "electric" in str(desc).lower():
            return key
    for key in _ELECTRIC_KEY_FALLBACKS:
        if key in service_to_desc:
            return key
    return "ELEC"


def fetch_account_list(host: str, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover service locations (meters) for the authenticated account.

    Calls GET /services/secured/user-data to get SmartHub-internal service
    location numbers, which are required by fetch_daily_generation.

    Returns list of dicts:
        {
            "service_location_number": str,  # SmartHub-internal meter identifier
            "account_number": str,           # utility account number (for poll request)
            "description": str,             # human-readable address/label
            "services": list[str],          # e.g. ["ELEC"]
        }

    UNVERIFIED: The exact nesting of accountNumber within the user-data response.
    HA integration confirms serviceLocationToUserDataServiceLocationSummaries
    is the right key. The accountNumber field name inside each summary needs
    verification against a real WEC or STOWE account.
    """
    url = f"{_base_url(host)}/services/secured/user-data"
    resp = httpx.get(
        url,
        params={"userId": session["primary_username"]},
        headers=_auth_headers(session["email"], session["auth_token"]),
        follow_redirects=True,
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()

    elec_key = _detect_electric_service_key(data)

    # UNVERIFIED: exact structure confirmed from HA integration source.
    # Maps location_id (str) → list of location summary objects.
    location_map: dict[str, Any] = (
        data.get("serviceLocationToUserDataServiceLocationSummaries") or {}
    )

    results: list[dict[str, Any]] = []
    for location_id, summaries in location_map.items():
        for summary in (summaries if isinstance(summaries, list) else [summaries]):
            services: list[str] = summary.get("services") or []
            if isinstance(services, list) and elec_key not in services:
                continue
            # UNVERIFIED: accountNumber field name inside summary.
            # The location_id itself is used as fallback.
            acct_no = (
                summary.get("accountNumber")
                or summary.get("account_number")
                or summary.get("id")
                or location_id
            )
            results.append({
                "service_location_number": location_id,
                "account_number": acct_no,
                "description": summary.get("description") or "",
                "services": services,
            })

    return results


def fetch_daily_generation(
    host: str,
    session: dict[str, Any],
    service_location: str,
    account_number: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Pull daily kWh totals from SmartHub for a net-metering account.

    Polls POST /services/secured/utility-usage/poll with DAILY timeFrame.
    Retries up to 3 times (5s apart) while status is PENDING.

    Returns list of dicts, one per day with data:
        {
            "day": date,
            "kwh_generated": float,   # energy returned to grid (RETURN channel)
            "kwh_consumed": float,    # energy consumed from grid (FORWARD channel)
            "kwh_net_export": float,  # positive = net exporter that day
        }

    For solar net-metering customers the relevant channel is RETURN (generation
    credited back) or NET (combined; negative = export). FORWARD = consumption.

    UNVERIFIED: flowDirection channel names for each VT co-op. VEC confirmed
    to expose FORWARD + RETURN. WEC and others need live verification.

    UNVERIFIED: timestamp format for x in response series data points.
    Assuming milliseconds (standard JS epoch). HA integration source notes
    suggest dividing by 1000 to get seconds — implemented below.
    """
    url = f"{_base_url(host)}/services/secured/utility-usage/poll"
    # Epoch milliseconds for the date range
    start_ms = int(
        datetime(start.year, start.month, start.day).timestamp() * 1000
    )
    end_ms = int(
        datetime(end.year, end.month, end.day, 23, 59, 59).timestamp() * 1000
    )

    body: dict[str, Any] = {
        "timeFrame": "DAILY",
        "userId": session["email"],
        "screen": "USAGE_EXPLORER",
        "includeDemand": False,
        "serviceLocationNumber": service_location,
        "accountNumber": account_number,
        "industries": ["ELECTRIC"],
        # UNVERIFIED: HA integration sends as string; Go implementation sends as int64.
        # Both appear accepted by the API.
        "startDateTime": str(start_ms),
        "endDateTime": str(end_ms),
    }

    headers = _auth_headers(session["email"], session["auth_token"])
    data: dict[str, Any] = {}
    for attempt in range(3):
        resp = httpx.post(
            url, json=body, headers=headers, follow_redirects=True, timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "COMPLETE":
            break
        if attempt < 2:
            time.sleep(5)

    electric_entries: list[dict] = (data.get("data") or {}).get("ELECTRIC") or []

    usage_entry: dict[str, Any] | None = None
    for entry in electric_entries:
        if entry.get("type") == "USAGE":
            usage_entry = entry
            break

    if usage_entry is None:
        return []

    # Build series name → data-point list
    series_map: dict[str, list] = {}
    for s in (usage_entry.get("series") or []):
        name = s.get("name")
        if name:
            series_map[name] = s.get("data") or []

    day_forward: dict[date, float] = {}
    day_return: dict[date, float] = {}
    day_net: dict[date, float] = {}

    for meter in (usage_entry.get("meters") or []):
        series_id: str = meter.get("seriesId") or ""
        flow: str = (meter.get("flowDirection") or "").upper()
        points: list = series_map.get(series_id) or []

        for pt in points:
            x = pt.get("x")
            y = pt.get("y")
            if x is None or y is None:
                continue
            # UNVERIFIED: x unit. Treating as milliseconds (ms → s → date).
            day = datetime.utcfromtimestamp(x / 1000.0).date()
            kwh = float(y)

            if flow == "FORWARD":
                day_forward[day] = day_forward.get(day, 0.0) + max(0.0, kwh)
            elif flow == "RETURN":
                day_return[day] = day_return.get(day, 0.0) + max(0.0, kwh)
            elif flow == "NET":
                day_net[day] = day_net.get(day, 0.0) + kwh

    # If NET is present (NET takes priority per HA integration), derive FORWARD/RETURN
    if day_net:
        for d, kwh in day_net.items():
            if kwh >= 0:
                day_forward.setdefault(d, kwh)
            else:
                day_return[d] = day_return.get(d, 0.0) + abs(kwh)

    all_days = sorted(set(list(day_forward.keys()) + list(day_return.keys())))
    return [
        {
            "day": d,
            "kwh_generated": round(day_return.get(d, 0.0), 4),
            "kwh_consumed": round(day_forward.get(d, 0.0), 4),
            "kwh_net_export": round(
                day_return.get(d, 0.0) - day_forward.get(d, 0.0), 4
            ),
        }
        for d in all_days
    ]


# ─── aria-label parsing (NISC SmartHub SPA format — uniform across deployments) ─

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
    if period_start > period_end:
        period_start = _parse_month_day(start_str, billing_year - 1)
    return period_start, period_end


def parse_usage(aria_label: str) -> dict[str, Any] | None:
    """Parse one usage-explorer SVG aria-label into a structured usage row.

    Returns None if the label does not match the NISC SmartHub format.
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
    dm = _BILL_DATE_RE.match((row.get("billing_date") or "").strip())
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


# ─── extension payload normalization ──────────────────────────────────────────

def parse_extension_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the Chrome extension SmartHub POST body into the standard shape
    expected by /v1/sync (provider, auth, user, accounts).

    Compatible with payloads from any SmartHub deployment (VEC, WEC, STOWE, etc.).
    The extension sets provider to the lowercase utility code detected from
    window.location.host (e.g. "wec" for washingtonelectric.smarthub.coop).

    The auth block may contain apiToken (authorizationToken captured by the
    extension's fetch-intercept) enabling server-side generation pulls.
    """
    provider = (payload.get("provider") or "vec").strip().lower()
    if provider not in ALL_SMARTHUB_PROVIDERS:
        # Unrecognized smarthub.coop host — treat as VEC for backward compat
        provider = "vec"

    raw_accounts = payload.get("accounts") or []
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for a in raw_accounts:
        acct_no = (a.get("accountNumber") or a.get("account_id") or "").strip()
        if not acct_no or acct_no in seen:
            continue
        seen.add(acct_no)
        svc_addr = a.get("serviceAddress")
        customer_name = a.get("customerName") or a.get("customer_name")
        accounts.append({
            "account_number": acct_no,
            "customer_number": None,
            "customer_name": customer_name,
            "nickname": customer_name,
            "service_address": (
                {"line1": svc_addr} if isinstance(svc_addr, str) else svc_addr
            ),
            "extra": {"provider": provider, "customerName": customer_name},
        })

    # Derive accounts from bill rows when the accounts list is empty
    if not accounts:
        for b in payload.get("bills") or []:
            acct_no = (b.get("account_id") or "").strip()
            if not acct_no or acct_no in seen:
                continue
            seen.add(acct_no)
            customer_name = b.get("customer_name")
            accounts.append({
                "account_number": acct_no,
                "customer_number": None,
                "customer_name": customer_name,
                "nickname": customer_name,
                "service_address": (
                    {"line1": b["service_address"]}
                    if b.get("service_address") else None
                ),
                "extra": {"provider": provider, "customerName": customer_name},
            })

    return {
        "provider": provider,
        "captured_at": payload.get("capturedAt"),
        "user": payload.get("user") or {},
        # Auth block: may contain apiToken if extension intercepted the login response
        "auth": payload.get("auth") or {},
        "accounts": accounts,
        "bills_raw": payload.get("bills") or [],
        "usage_raw": payload.get("usage") or [],
    }
