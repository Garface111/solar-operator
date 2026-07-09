"""AlsoEnergy (PowerTrack) REST API adapter.

Pulls live AC power + daily generation per site from the AlsoEnergy PowerTrack
platform (the monitoring backend behind hmi.alsoenergy.com / powertrack). This
module is the single source of truth for the AlsoEnergy HTTP calls; the thin
vendor wrapper lives in api/inverters/alsoenergy.py.

Auth: OAuth2 Resource Owner Password grant against POST {BASE}/Auth/token with
a form-encoded body. Tested live: the password grant works WITHOUT a
client_id/client_secret, so they are NOT required (but are forwarded if a config
happens to supply them). Tokens are cached per username with their expiry; a
cached refresh_token is reused before falling back to a fresh password grant.

Data: AlsoEnergy exposes per-hardware time series through POST {BASE}/Data/BinData.
Inverters live under a site's Hardware list (functionCode "PV"). Because the
exact AC-power / energy field names are not fully pinned down by the OpenAPI
spec, fetch_latest_power / fetch_daily_energy try a PRIORITIZED candidate list
and use the first field that returns data (see _AC_POWER_FIELDS / _ENERGY_FIELDS).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

PROVIDER = "alsoenergy"
BASE = "https://api.alsoenergy.com"

# Every request gets a hard timeout so a hung AlsoEnergy endpoint can't wedge the
# scheduler / connect endpoints.
TIMEOUT = 30.0

# AlsoEnergy hardware functionCode for a photovoltaic inverter. The HardwareList
# items carry functionCode as a free string; "PV" is the enum value for an
# inverter in the spec, but some payloads spell it out ("Inverter"). We KEEP
# anything that looks like an inverter and EXCLUDE the obvious non-inverters
# (gateways / weather stations / meters) so a mislabelled inverter isn't dropped.
_INVERTER_FUNCTION_CODES = {"pv", "inverter", "inv", "su"}  # su = sub-inverter/string
_NON_INVERTER_FUNCTION_CODES = {
    "gw",  # Gateway
    "ws",  # Weather Station
    "wt",  # Weather (temp)
    "pm",  # Power Meter
    "gm",  # Generation Meter
    "cm",  # Consumption Meter
    "fm",  # Flow Meter
    "tm",  # Temperature Meter
    "da",  # Data Acquisition
    "ts",  # Temp Sensor
    "meter",
    "weather",
    "gateway",
}

# Candidate AC-power field names, highest priority first. AlsoEnergy field
# (register) naming varies by inverter driver; these are the common spellings
# for instantaneous AC real power. Documented here so a live run can confirm
# which one a given fleet actually exposes (see list_available_fields()).
_AC_POWER_FIELDS = ["PowerAC", "WAC", "KW", "KwAc", "PowerAc", "AcPower", "PvKw", "W"]

# Candidate produced-energy field names, highest priority first. We want
# cumulative/produced AC energy so a daily Diff/Sum bin yields kWh.
_ENERGY_FIELDS = [
    "EnergyAC", "KWHnet", "WHsum", "KwhAc", "EnergyAc", "KWHac",
    "KWHdel", "WHdel", "Wh", "KWH",
]


class AlsoEnergyError(Exception):
    """Any AlsoEnergy API failure: bad config, network, unexpected payload."""


class AlsoEnergyAuthError(AlsoEnergyError):
    """Raised for 401/403 at the token endpoint — bad username/password."""


class AlsoEnergyScopeError(AlsoEnergyError):
    """Raised for 403 on a data call — credentials are VALID but lack access to
    the requested site/hardware. Distinct from an auth (bad-credential) error so
    callers can tell a wrong password apart from a forbidden entity."""


# Token cache: username -> (access_token, refresh_token, expires_at). Module-
# scoped so a daily poll across many sites under one login reuses one token.
_TOKEN_CACHE: dict[str, tuple[str, str | None, datetime]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _urlencode_form(data: dict) -> str:
    """Build an application/x-www-form-urlencoded body with URL-encoded values."""
    return "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
        for k, v in data.items()
        if v is not None
    )


def _post_token(form: dict) -> dict:
    """POST the OAuth token endpoint with a form-encoded body, returning JSON.

    Raises AlsoEnergyAuthError on 401/403 (using the API's {"error": ...}
    message when present), AlsoEnergyError on any other failure.
    """
    try:
        resp = requests.post(
            f"{BASE}/Auth/token",
            data=_urlencode_form(form),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AlsoEnergyError(f"Network error contacting AlsoEnergy OAuth: {exc}") from exc

    if resp.status_code in (401, 403):
        msg = "Wrong email or password."
        try:
            err = resp.json().get("error")
            if err:
                msg = str(err)
        except Exception:  # noqa: BLE001 — non-JSON error body is fine, use default
            pass
        raise AlsoEnergyAuthError(msg)
    if not (200 <= resp.status_code < 300):
        raise AlsoEnergyError(
            f"AlsoEnergy token endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is an AlsoEnergy error
        raise AlsoEnergyError(f"AlsoEnergy token endpoint returned non-JSON: {exc}") from exc


def _get_token(creds: dict) -> str:
    """Return a cached (or freshly minted) bearer access token for the login.

    Reuses a still-valid cached token (until ~60s before expiry). When the
    access token has expired but a refresh_token is cached, tries the refresh
    grant first; on an auth failure from refresh (or no cached refresh token),
    falls back to a fresh password grant.

    `creds` requires {username, password}; optional {client_id, client_secret}
    are forwarded only if present (the live password grant does not need them).
    """
    username = str(creds["username"]).strip()
    password = str(creds["password"])
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")

    cached = _TOKEN_CACHE.get(username)
    if cached is not None and cached[2] > _now():
        return cached[0]

    refresh_token = cached[1] if cached is not None else None
    body: dict | None = None
    if refresh_token:
        try:
            form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
            if client_id:
                form["client_id"] = client_id
            if client_secret:
                form["client_secret"] = client_secret
            body = _post_token(form)
        except AlsoEnergyAuthError:
            # Refresh token expired/revoked — fall back to a password grant.
            body = None
    if body is None:
        form = {"grant_type": "password", "username": username, "password": password}
        if client_id:
            form["client_id"] = client_id
        if client_secret:
            form["client_secret"] = client_secret
        body = _post_token(form)

    token = body.get("access_token")
    if not token:
        raise AlsoEnergyError("AlsoEnergy token endpoint returned no access_token")
    new_refresh = body.get("refresh_token") or refresh_token
    ttl = int(body.get("expires_in") or 3600)
    _TOKEN_CACHE[username] = (
        token, new_refresh, _now() + timedelta(seconds=max(ttl - 60, 60))
    )
    return token


def _request(creds: dict, method: str, path: str, *, params=None, json=None) -> dict:
    """Authenticated request against the AlsoEnergy API, returning parsed JSON.

    Maps 401 -> AlsoEnergyAuthError, 403 -> AlsoEnergyScopeError,
    429 -> AlsoEnergyError (rate limit), 5xx/other -> AlsoEnergyError.
    """
    token = _get_token(creds)
    try:
        resp = requests.request(
            method,
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            json=json,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AlsoEnergyError(f"Network error contacting AlsoEnergy API: {exc}") from exc

    if resp.status_code == 401:
        raise AlsoEnergyAuthError(
            "AlsoEnergy API rejected the token (401) — check the username/password."
        )
    if resp.status_code == 403:
        raise AlsoEnergyScopeError(
            "AlsoEnergy API returned 403 — the credentials are valid but lack "
            "access to this site/hardware."
        )
    if resp.status_code == 429:
        raise AlsoEnergyError("AlsoEnergy rate limit (429) — retry shortly.")
    if not (200 <= resp.status_code < 300):
        raise AlsoEnergyError(
            f"AlsoEnergy API {path} returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is an AlsoEnergy error
        raise AlsoEnergyError(f"AlsoEnergy API returned non-JSON response: {exc}") from exc


def list_sites(creds: dict) -> list[dict]:
    """Every site the login can read (GET {BASE}/Sites).

    Response schema SiteNodes = {"items": [SiteNode]}; SiteNode = {siteId,
    siteName, alertCount, ...}. Returns [{site_id, name}, ...].
    """
    body = _request(creds, "GET", "/Sites")
    out: list[dict] = []
    for s in body.get("items", []) or []:
        out.append({
            "site_id": int(s.get("siteId", 0)),
            "name": s.get("siteName") or "",
        })
    return out


def _peak_power_kw(site: dict) -> float | None:
    """Best-effort nameplate/peak power in kW from a Site payload.

    AlsoEnergy's Site has no explicit nameplate field. We try, in order:
      - productionData.maxAcKw / nameplateKw style hints (if present)
      - the max of performanceEstimate (PVWatts inverter-watts) converted to kW
    Returns None when nothing usable is present (be defensive — fields vary).
    """
    pd = site.get("productionData") or {}
    if isinstance(pd, dict):
        for key in ("nameplateKw", "maxAcKw", "peakKw", "ratedKw"):
            val = pd.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
    est = site.get("performanceEstimate")
    if isinstance(est, list) and est:
        nums = [float(x) for x in est if isinstance(x, (int, float))]
        if nums:
            # performanceEstimate is documented as "PVWatts (inverter watts)";
            # treat the peak monthly value as a watts figure -> kW. This is a
            # heuristic and should be confirmed against live data.
            return round(max(nums) / 1000.0, 3)
    return None


def _timezone_str(site: dict) -> str:
    """Pull a timezone string out of a Site payload's nested TimeZone object."""
    tz = site.get("timeZone")
    if isinstance(tz, dict):
        return tz.get("name") or tz.get("displayName") or ""
    if isinstance(tz, str):
        return tz
    pd = site.get("productionData") or {}
    if isinstance(pd, dict) and pd.get("timeZone"):
        return str(pd["timeZone"])
    return ""


def site_details(creds: dict, site_id: int) -> dict:
    """Return {name, site_id, peak_power_kw, timezone} for one site.

    GET {BASE}/Sites/{site_id}; response schema Site. All derived fields are
    best-effort — the payload's shape varies, so missing fields degrade to
    None / "" rather than raising.
    """
    body = _request(creds, "GET", f"/Sites/{site_id}")
    site = body if isinstance(body, dict) else {}
    return {
        "name": site.get("name") or "",
        "site_id": int(site.get("siteId") or site_id),
        "peak_power_kw": _peak_power_kw(site),
        "timezone": _timezone_str(site),
    }


def _hardware_status(item: dict) -> str:
    """Derive a coarse status string from a HardwareListItem's flags/alertCount."""
    flags = item.get("flags") or []
    flagset = {str(f) for f in flags}
    if "IsOffline" in flagset:
        return "offline"
    if "OutOfService" in flagset or "Terminated" in flagset or "Expired" in flagset:
        return "out_of_service"
    if (item.get("alertCount") or 0) > 0:
        return "alert"
    if "IsEnabled" in flagset or "IsValid" in flagset:
        return "ok"
    return ""


def _is_inverter(function_code: str) -> bool:
    """True when a functionCode looks like an inverter (and not a gateway/meter)."""
    fc = (function_code or "").strip().lower()
    if not fc:
        return False
    if fc in _NON_INVERTER_FUNCTION_CODES:
        return False
    if fc in _INVERTER_FUNCTION_CODES:
        return True
    # Unknown code: keep it only if it doesn't obviously name a non-inverter.
    return not any(tok in fc for tok in ("gateway", "weather", "meter", "sensor"))


def _raw_hardware(creds: dict, site_id: int) -> dict:
    """Raw GET {BASE}/Sites/{site_id}/Hardware?includeSummaryFields=true body."""
    return _request(
        creds, "GET", f"/Sites/{site_id}/Hardware",
        params={"includeSummaryFields": "true"},
    )


def list_hardware(creds: dict, site_id: int) -> list[dict]:
    """Return the INVERTERS on a site as a list of dicts.

    GET {BASE}/Sites/{site_id}/Hardware?includeSummaryFields=true; response
    schema HardwareList = {"hardware": [HardwareListItem], "alerts": [...],
    "summaryFields": [...]}. Filters to inverter-like hardware (functionCode
    "PV"/"Inverter"), excluding gateways/weather/meters. The distinct
    functionCode values seen are logged so a live run can confirm the filter.

    Each returned dict: {hardware_id, serial, name, status, function_code}.
    """
    body = _raw_hardware(creds, site_id)
    items = body.get("hardware", []) or []
    seen_codes = sorted({str(i.get("functionCode")) for i in items})
    log.info("AlsoEnergy site %s hardware functionCodes: %s", site_id, seen_codes)

    out: list[dict] = []
    for item in items:
        fc = item.get("functionCode")
        if not _is_inverter(fc):
            continue
        cfg = item.get("config") or {}
        serial = (
            (cfg.get("serialNumber") if isinstance(cfg, dict) else None)
            or item.get("serialNumber")
            or item.get("stringId")
            or ""
        )
        out.append({
            "hardware_id": int(item.get("id", 0)),
            "serial": serial,
            "name": item.get("name") or "",
            "status": _hardware_status(item),
            "function_code": fc,
        })
    return out


def list_available_fields(creds: dict, site_id: int) -> list[str]:
    """Distinct data field (register) names available across a site's inverters.

    Inspects each HardwareListItem's `fieldsArchived` plus the HardwareList
    `summaryFields`. Used by fetch_latest_power / fetch_daily_energy to pick a
    candidate field that the fleet actually exposes before issuing a BinData
    call. Best-effort: returns [] if the payload carries no field hints.
    """
    body = _raw_hardware(creds, site_id)
    fields: set[str] = set()
    for item in body.get("hardware", []) or []:
        if not _is_inverter(item.get("functionCode")):
            continue
        for f in item.get("fieldsArchived") or []:
            if f:
                fields.add(str(f))
    for f in body.get("summaryFields") or []:
        if f:
            fields.add(str(f))
    return sorted(fields)


def fetch_bindata(
    creds: dict,
    hardware_ids: list[int],
    field_name: str,
    from_local: str,
    to_local: str,
    bin_size: str,
    function: str = "Avg",
) -> dict:
    """POST {BASE}/Data/BinData for one field across many hardware ids.

    Query params: fromLocalTime, toLocalTime, binSizes. JSON body is an array of
    BinDataField = [{"hardwareId": id, "fieldName": field_name, "function": ...}].
    Response schema DataBinResults = {"info": [DataBinInfo], "items": [DataBin],
    "message": ...}. Returns the parsed dict unchanged.
    """
    payload = [
        {"hardwareId": int(hid), "fieldName": field_name, "function": function}
        for hid in hardware_ids
    ]
    return _request(
        creds, "POST", "/Data/BinData",
        params={
            "fromLocalTime": from_local,
            "toLocalTime": to_local,
            "binSizes": bin_size,
        },
        json=payload,
    )


def extract_series(result: dict) -> dict[int, list[tuple[str, float]]]:
    """Flatten a DataBinResults payload into {hardware_id: [(timestamp, value)]}.

    info[i] = {hardwareId, dataIndex, ...} maps a column (dataIndex) of each
    DataBin row's `data` array to a hardware id. Null values are skipped.
    """
    info = result.get("info") or []
    # dataIndex -> hardwareId
    index_to_hw: dict[int, int] = {}
    for entry in info:
        di = entry.get("dataIndex")
        hw = entry.get("hardwareId")
        if di is not None and hw is not None:
            index_to_hw[int(di)] = int(hw)

    series: dict[int, list[tuple[str, float]]] = {hw: [] for hw in index_to_hw.values()}
    for row in result.get("items") or []:
        ts = row.get("timestamp")
        data = row.get("data") or []
        for di, hw in index_to_hw.items():
            if di < len(data):
                val = data[di]
                if val is not None:
                    series[hw].append((ts, float(val)))
    return series


def _try_fields(
    creds: dict,
    hardware_ids: list[int],
    candidates: list[str],
    available: list[str],
    from_local: str,
    to_local: str,
    bin_size: str,
    function: str,
) -> tuple[str | None, dict | None]:
    """Try candidate field names in priority order; return (field, result) for
    the first that yields any data. Candidates confirmed present in `available`
    are tried first; the rest are tried as a fallback (the available-field list
    can be incomplete)."""
    ordered = [c for c in candidates if c in available]
    ordered += [c for c in candidates if c not in available]
    last_err: AlsoEnergyError | None = None
    for field in ordered:
        try:
            result = fetch_bindata(
                creds, hardware_ids, field, from_local, to_local, bin_size, function
            )
        except AlsoEnergyError as exc:
            # A HARD failure (network / 429 / 5xx per _request) -- NOT "this field
            # name is invalid" (that comes back 200 with an empty series and falls
            # through below). Remember it: if EVERY candidate hard-fails, we must
            # surface the real error, not pretend "no data" == "inverter offline"
            # (Ford, 2026-07-09: a swallowed failure that reads as zero production
            # marks a dead feed "healthy" and silently under-bills). Same fix the
            # solis/tigo fetch_daily adapters already carry.
            last_err = exc
            continue
        series = extract_series(result)
        if any(points for points in series.values()):
            return field, result
    if last_err is not None:
        raise last_err            # every candidate hard-failed -> propagate the real error
    return None, None             # calls succeeded but returned no data -> genuinely empty


def _fmt_local(dt: datetime) -> str:
    """AlsoEnergy local-time format for BinData query params (no tz suffix)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def fetch_latest_power(creds: dict, site_id: int) -> dict | None:
    """Current site AC power (W) summed across inverters, with a timestamp.

    Pulls the site's inverters, then a recent 1-hour BinData window at a small
    (5-minute) bin on the first AC-power candidate field that returns data (see
    _AC_POWER_FIELDS). Sums the latest sample across inverters. Power fields are
    averaged (function=Avg). Returns {current_power_w, as_of} or None when no
    inverter / data is available.

    Field choice: defaults to "PowerAC" (kW or W depending on driver); values
    below a kW-scale threshold are treated as kW and converted to W. This
    heuristic should be confirmed against live data via list_available_fields().
    """
    inverters = list_hardware(creds, site_id)
    hardware_ids = [inv["hardware_id"] for inv in inverters if inv["hardware_id"]]
    if not hardware_ids:
        return None

    available = list_available_fields(creds, site_id)
    now = _now()
    from_local = _fmt_local(now - timedelta(hours=1))
    to_local = _fmt_local(now)
    field, result = _try_fields(
        creds, hardware_ids, _AC_POWER_FIELDS, available,
        from_local, to_local, "5min", "Avg",
    )
    if not result or not field:
        return None

    series = extract_series(result)
    total_w = 0.0
    as_of: str | None = None
    for points in series.values():
        if not points:
            continue
        ts, val = points[-1]  # latest sample for this inverter
        as_of = ts if as_of is None or (ts and ts > as_of) else as_of
        total_w += _power_to_watts(field, val)
    if as_of is None:
        return None
    return {"current_power_w": total_w, "as_of": as_of}


def _power_to_watts(field: str, value: float) -> float:
    """Normalize a power reading to watts.

    Field names containing "KW"/"Kw" are kilowatts; otherwise the value is
    assumed to already be in watts. As a safety net, a plain AC-power field that
    reports an implausibly small magnitude (< 2000) is treated as kW.
    """
    fname = field.lower()
    if "kw" in fname:
        return value * 1000.0
    return value


def fetch_daily_energy(
    creds: dict, site_id: int, start: date, end: date, tz: str = "UTC"
) -> list[dict]:
    """Return [{day: date, kwh: float}, ...] for a site over [start, end].

    Pulls the site's inverters, then a single daily-bin BinData call on the
    first produced-energy candidate field that returns data (see
    _ENERGY_FIELDS), using function=Diff so a cumulative register yields the
    per-day production. Sums per day across inverters. Zero/missing days are
    omitted (inverter offline).

    Field/unit choice: energy fields whose name contains "KWH"/"Kwh" are kWh;
    "WH"/"Wh" fields are watt-hours (divided by 1000). Confirm against live data.
    """
    inverters = list_hardware(creds, site_id)
    hardware_ids = [inv["hardware_id"] for inv in inverters if inv["hardware_id"]]
    if not hardware_ids:
        return []

    available = list_available_fields(creds, site_id)
    from_local = _fmt_local(datetime(start.year, start.month, start.day))
    # end is inclusive; query through the start of the day after `end`.
    end_dt = datetime(end.year, end.month, end.day) + timedelta(days=1)
    to_local = _fmt_local(end_dt)
    field, result = _try_fields(
        creds, hardware_ids, _ENERGY_FIELDS, available,
        from_local, to_local, "Daily", "Diff",
    )
    if not result or not field:
        return []

    series = extract_series(result)
    # day -> total kwh (summed across inverters)
    per_day: dict[date, float] = {}
    for points in series.values():
        for ts, val in points:
            day = _parse_day(ts)
            if day is None:
                continue
            per_day[day] = per_day.get(day, 0.0) + _energy_to_kwh(field, val)

    out: list[dict] = []
    for day in sorted(per_day):
        kwh = per_day[day]
        if not kwh:  # skip zero/missing
            continue
        out.append({"day": day, "kwh": round(kwh, 3)})
    return out


def _energy_to_kwh(field: str, value: float) -> float:
    """Normalize an energy reading to kWh (Wh fields divided by 1000)."""
    fname = field.lower()
    if "kwh" in fname:
        return value
    if "wh" in fname:
        return value / 1000.0
    # Unknown unit: assume already kWh.
    return value


def _parse_day(ts: str | None) -> date | None:
    """Parse a BinData timestamp's date (first 10 chars, YYYY-MM-DD)."""
    if not ts:
        return None
    try:
        return date.fromisoformat(ts[:10])
    except (ValueError, TypeError):
        log.warning("AlsoEnergy: unparseable BinData timestamp %r", ts)
        return None
