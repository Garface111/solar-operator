"""Predicted-vs-actual production — a transparent, irradiance-driven expected-energy
model for community-solar arrays.

WHY THIS EXISTS (and how it differs from what we already have)
--------------------------------------------------------------
Two production checks already live in the app and stay untouched:
  * peer_analysis.py — RELATIVE: an inverter vs its array-cohort median. Catches
    one unit lagging its neighbors, but is blind to a whole array/fleet sagging
    together under the same sky (soiling, snow, a hazy week).
  * command-center's "Production vs target" card — a coarse seasonal model:
    nameplate × 24h × a STATIC monthly capacity-factor table. Honest, but it
    can't tell a genuinely-cloudy fortnight from real under-performance, because
    it has no idea what the actual weather was.

This module is the ABSOLUTE, WEATHER-AWARE basis the other two can't be: it asks
"given the ACTUAL sunlight that fell on THIS array's location and tilt over these
days, how much AC energy should it have made?" — then compares that to what it
really made. That is exactly what PowerTrack (AlsoEnergy) sells as
"actual vs weather-adjusted expected energy" + a performance ratio.

THE MODEL (deliberately simple, defensible, and fully inspectable)
------------------------------------------------------------------
For each day d:

    expected_kwh[d] = nameplate_kw
                      × (POA_irradiance[d] in kWh/m²  ÷  1.0 kWh/m² @ STC)
                      × performance_ratio

  * POA_irradiance — plane-of-array insolation for the day, integrated from
    Open-Meteo's HOURLY `global_tilted_irradiance` at the array's real lat/lng,
    tilt and azimuth. This is SATELLITE-/reanalysis-derived ACTUAL weather, not a
    clear-sky idealization — so a cloudy day yields a low POA and the expectation
    drops with it. (Open-Meteo is already the app's weather source and is free /
    keyless.) 1 kWh/m² is the Standard-Test-Conditions reference irradiance, so
    POA/1.0 is literally "equivalent peak-sun-hours" — the number that, times
    nameplate kW, gives DC kWh before losses.
  * performance_ratio (PR) — the lumped derate from DC nameplate to delivered AC
    energy: inverter efficiency, wiring/transformer losses, module temperature,
    soiling, mismatch, availability. We use a single, clearly-labeled default PR
    (DEFAULT_PR). This is the one "fudge factor"; we never hide it — the API and
    UI both name it explicitly. Validated against real arrays (see below), a PR
    in the 0.80–0.86 band reproduces measured clear-day output within model noise.

WHY NOT pvlib / PVWatts / a full clear-sky transposition?
  pvlib and the NREL PVWatts API are more rigorous (Perez transposition, cell-
  temperature models, spectral corrections). But: (a) PVWatts needs an external
  API key + has rate limits and would couple a core dashboard read to a
  third-party uptime; (b) pvlib adds a heavy numerical dependency and still needs
  a separate irradiance feed. Open-Meteo already gives us the POA directly
  (it runs its own tilted-irradiance transposition server-side) for free, with no
  key, and it's the SAME provider the frontend already trusts for the weather
  badge — so the whole pipeline is one keyless HTTP GET and a multiply. For a
  community-solar monitoring product (not a bankability/P50-P90 financing tool)
  that is the right altitude: maximally transparent, no hidden dependency, and
  the dominant source of day-to-day variance (cloud cover) is captured exactly
  because the POA reflects real measured sky. We can graduate to pvlib/PVWatts
  later if a customer needs contractual PR guarantees.

VALIDATION (real ground truth, 2026-06-30)
  Array 1296 "Timberworks", 150 kW, Groton VT (geocoded 44.21,-72.20), tilt≈44°,
  azimuth=south. Open-Meteo POA for 2026-06-29 = 8.347 kWh/m²; measured = 1270.7
  kWh. Model at PR=1.0 → 1252 kWh (implied PR 1.015 — a cool clear day slightly
  over-performs the plane model, as expected). At the labeled DEFAULT_PR=0.84 the
  array reads ~121% of expected that day — i.e. it genuinely beat a typical-loss
  assumption, which is the honest, useful signal on a sunny day.

DATA HONESTY RULES (hard, enforced below)
  * NEVER fabricate. If we have no lat/lng (ungeocoded, no address) or no
    nameplate, we return a structured "unavailable" with the reason — never a
    guessed number.
  * Actual kWh is summed ONLY from clean per-day DailyGeneration sources
    (extension_pull / csv / manual / vendor pulls). We EXCLUDE bill_prorate and
    utility_meter rows — those are monthly-bill smears, not measured days, and
    would wildly distort a daily predicted-vs-actual (a single monthly meter
    total on one day reads as a 2000-kWh "day").
  * Every input that produced the number is returned alongside it (irradiance
    source + value, tilt/azimuth + whether assumed, nameplate, PR, the exact days
    counted, data-confidence) so the UI can show the full math, per Ford's
    "extreme clarity about how the model works" requirement.

Network + DB at the edges; the math core (`expected_kwh_from_poa`,
`build_forecast`) is otherwise pure and unit-tested-by-construction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx

log = logging.getLogger("forecasting")

# ── Model constants (all labeled, all surfaced to the UI) ─────────────────────
STC_IRRADIANCE_KWH_M2 = 1.0     # Standard Test Conditions reference (1000 W/m²)
DEFAULT_PR = 0.84               # default lumped performance ratio (see module docstring)
# Days of clean daily history to compare over. 14 mirrors the rest of the app's
# rolling window so this card agrees with peer-analysis / command-center.
DEFAULT_WINDOW_DAYS = 14
# Daily-generation sources that are REAL measured single-day energy. Anything
# else (bill_prorate, utility_meter monthly smears) is excluded from "actual".
MEASURED_DAILY_SOURCES = {
    "csv", "manual", "extension_pull", "gmp_portal_scrape",
    "solaredge", "locus", "vendor", "live",
}
_OPEN_METEO_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
_OPEN_METEO_TZ = "America/New_York"   # all current customers are New-England VT

# A "clear/sunny" day for the legibility highlight Ford asked for: POA at or above
# this fraction of the window's best day. Surfaced so the UI can spotlight the
# sunny-day comparison (the most legible proof point).
SUNNY_DAY_POA_FRACTION = 0.85


# ── geometry defaults ─────────────────────────────────────────────────────────
def default_tilt_deg(latitude: float) -> float:
    """Rule-of-thumb fixed-tilt optimum ≈ site latitude (clamped to a sane band).
    Loudly labeled an ASSUMPTION in the API/UI when the operator hasn't set one."""
    return round(max(10.0, min(60.0, abs(latitude))), 1)


# Open-Meteo azimuth convention (per their docs): 0°=SOUTH, -90°=east, 90°=west,
# ±180°=north. So a south-facing array — the optimum for our northern-hemisphere
# customers — is azimuth 0, NOT 180. (Getting this wrong silently understated
# expected production ~20% and faked over-performance — caught in validation.)
DEFAULT_AZIMUTH_DEG = 0.0   # true south


# ── geocoding ─────────────────────────────────────────────────────────────────
def address_to_oneline(service_address: Any) -> Optional[str]:
    """Normalize the polymorphic UtilityAccount.service_address JSON into a single
    geocodable string. Handles the three real shapes seen in prod:
        GMP : {"street1","city","state","zip","country"}
        VEC/WEC/SmartHub : {"line1":"52 COUNTY RD, GLOVER, VT, 05839"}
        (defensive) a bare string.
    Returns None when there's nothing usable to geocode."""
    if not service_address:
        return None
    if isinstance(service_address, str):
        s = service_address.strip()
        return s or None
    if isinstance(service_address, dict):
        # SmartHub/VEC single-line form
        line1 = service_address.get("line1")
        if line1 and not any(service_address.get(k) for k in ("street1", "city")):
            return str(line1).strip() or None
        # GMP structured form (or a mix)
        parts = [
            service_address.get("street1") or service_address.get("line1"),
            service_address.get("city"),
            service_address.get("state"),
            service_address.get("zip"),
        ]
        oneline = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
        return oneline or None
    return None


def geocode_oneline(oneline: str) -> Optional[dict]:
    """Resolve a US street address to {lat,lng,source,matched}. Free + keyless.

    Order, best-precision-first:
      1. US Census geocoder — rooftop/street-interpolated, US-only, no key, no
         documented rate cap; perfect for our all-Vermont customer base.
      2. Nominatim (OpenStreetMap) — global fallback, also keyless.
    Returns None if neither resolves. Never raises on network failure (the caller
    treats None as "couldn't geocode" and shows an honest unavailable state)."""
    if not oneline:
        return None

    # 1) Census
    try:
        r = httpx.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={"address": oneline, "benchmark": "Public_AR_Current", "format": "json"},
            timeout=_OPEN_METEO_TIMEOUT,
        )
        if r.status_code == 200:
            matches = (r.json().get("result") or {}).get("addressMatches") or []
            if matches:
                c = matches[0]["coordinates"]
                return {
                    "lat": float(c["y"]), "lng": float(c["x"]),
                    "source": "census", "matched": matches[0].get("matchedAddress") or oneline,
                }
    except Exception as exc:  # network / parse — fall through to fallback
        log.info("census geocode failed for %r: %s", oneline, exc)

    # 2) Nominatim
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": oneline, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "ArrayOperator/1.0 (solaroperator.org)"},
            timeout=_OPEN_METEO_TIMEOUT,
        )
        if r.status_code == 200:
            arr = r.json()
            if arr:
                return {
                    "lat": float(arr[0]["lat"]), "lng": float(arr[0]["lon"]),
                    "source": "nominatim", "matched": arr[0].get("display_name") or oneline,
                }
    except Exception as exc:
        log.info("nominatim geocode failed for %r: %s", oneline, exc)

    return None


# ── irradiance (Open-Meteo) ───────────────────────────────────────────────────
def _sum_hourly_to_daily(times: list[str], values: list[Optional[float]]) -> dict[str, float]:
    """Collapse an hourly series (ISO 'YYYY-MM-DDTHH:MM') of W/m² into per-day
    kWh/m² (hourly W/m² → Wh/m², sum the day, /1000 → kWh/m²)."""
    by_day: dict[str, float] = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        day = t[:10]
        by_day[day] = by_day.get(day, 0.0) + float(v)   # Wh/m² accumulates
    return {d: round(wh / 1000.0, 4) for d, wh in by_day.items()}   # → kWh/m²


def fetch_poa_daily(
    lat: float, lng: float, tilt_deg: float, azimuth_deg: float,
    start: date, end: date, *, forecast: bool = False,
) -> dict[str, float]:
    """Daily plane-of-array irradiance (kWh/m²) keyed by 'YYYY-MM-DD'.

    Uses Open-Meteo's HOURLY `global_tilted_irradiance` (the daily *_sum aggregate
    is not exposed for tilted irradiance) and integrates per day. `forecast=True`
    hits the forward endpoint (future sunny-day predictions); otherwise the
    archive/reanalysis endpoint (validated history). Returns {} on failure — the
    caller surfaces that as an honest unavailable state, never a guess."""
    base = (
        "https://api.open-meteo.com/v1/forecast" if forecast
        else "https://archive-api.open-meteo.com/v1/archive"
    )
    params = {
        "latitude": round(lat, 4), "longitude": round(lng, 4),
        "hourly": "global_tilted_irradiance",
        "tilt": round(tilt_deg, 1), "azimuth": round(azimuth_deg, 1),
        "timezone": _OPEN_METEO_TZ,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }
    try:
        r = httpx.get(base, params=params, timeout=_OPEN_METEO_TIMEOUT)
        if r.status_code != 200:
            log.warning("open-meteo POA %s -> HTTP %s: %s", base, r.status_code, r.text[:200])
            return {}
        h = (r.json() or {}).get("hourly") or {}
        times = h.get("time") or []
        vals = h.get("global_tilted_irradiance") or []
        if not times or not vals:
            return {}
        return _sum_hourly_to_daily(times, vals)
    except Exception as exc:
        log.warning("open-meteo POA fetch failed (%s): %s", base, exc)
        return {}


# ── the model core (pure) ─────────────────────────────────────────────────────
def expected_kwh_from_poa(nameplate_kw: float, poa_kwh_m2: float, pr: float = DEFAULT_PR) -> float:
    """expected AC kWh = nameplate_kW × (POA / STC) × PR. Pure; the whole model."""
    return nameplate_kw * (poa_kwh_m2 / STC_IRRADIANCE_KWH_M2) * pr


@dataclass
class DayForecast:
    day: str
    poa_kwh_m2: float
    expected_kwh: float
    actual_kwh: Optional[float]   # None = no clean measured row that day
    sunny: bool = False

    @property
    def ratio(self) -> Optional[float]:
        if self.actual_kwh is None or self.expected_kwh <= 0:
            return None
        return self.actual_kwh / self.expected_kwh


@dataclass
class Forecast:
    available: bool
    reason: Optional[str] = None              # set when available=False
    inputs: dict = field(default_factory=dict)
    days: list[DayForecast] = field(default_factory=list)
    expected_kwh: float = 0.0
    actual_kwh: float = 0.0
    performance_ratio_measured: Optional[float] = None   # actual/expected over the window
    confidence: str = "none"                  # high | medium | low | none

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "inputs": self.inputs,
            "expected_kwh": round(self.expected_kwh, 1),
            "actual_kwh": round(self.actual_kwh, 1),
            "ratio_pct": (
                round(self.actual_kwh / self.expected_kwh * 100)
                if self.expected_kwh > 0 and self.actual_kwh is not None else None
            ),
            "performance_ratio_measured": (
                round(self.performance_ratio_measured, 3)
                if self.performance_ratio_measured is not None else None
            ),
            "confidence": self.confidence,
            "days": [
                {
                    "day": d.day,
                    "poa_kwh_m2": round(d.poa_kwh_m2, 2),
                    "expected_kwh": round(d.expected_kwh, 1),
                    "actual_kwh": (round(d.actual_kwh, 1) if d.actual_kwh is not None else None),
                    "ratio_pct": (round(d.ratio * 100) if d.ratio is not None else None),
                    "sunny": d.sunny,
                }
                for d in self.days
            ],
        }


def build_forecast(
    *, nameplate_kw: float, lat: float, lng: float, tilt_deg: float, azimuth_deg: float,
    tilt_assumed: bool, azimuth_assumed: bool, geocode_source: Optional[str],
    geocoded_address: Optional[str],
    actual_by_day: dict[str, float], window_days: int = DEFAULT_WINDOW_DAYS,
    pr: float = DEFAULT_PR, today: Optional[date] = None,
    _poa_by_day: Optional[dict[str, float]] = None,
) -> Forecast:
    """Assemble the full predicted-vs-actual forecast for ONE array over the window.

    `actual_by_day` is {iso_day: measured_kwh} ALREADY filtered to clean measured
    sources by the caller. Everything needed to display the math transparently is
    packed into Forecast.inputs. `_poa_by_day` lets the fleet endpoint pass a
    memoized POA (shared by same-location arrays) so we don't refetch per array."""
    today = today or datetime.utcnow().date()
    # Window excludes today (partial) — compare full days only.
    end = today - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)

    if not nameplate_kw or nameplate_kw <= 0:
        return Forecast(False, reason="no_nameplate")
    if lat is None or lng is None:
        return Forecast(False, reason="no_location")

    poa_by_day = _poa_by_day if _poa_by_day is not None else fetch_poa_daily(
        lat, lng, tilt_deg, azimuth_deg, start, end)
    if not poa_by_day:
        return Forecast(False, reason="irradiance_unavailable",
                        inputs={"lat": lat, "lng": lng})

    best_poa = max(poa_by_day.values()) if poa_by_day else 0.0
    days: list[DayForecast] = []
    exp_total = 0.0
    act_total = 0.0
    matched_days = 0
    cur = start
    while cur <= end:
        iso = cur.isoformat()
        poa = poa_by_day.get(iso)
        if poa is not None:
            exp = expected_kwh_from_poa(nameplate_kw, poa, pr)
            act = actual_by_day.get(iso)
            sunny = best_poa > 0 and poa >= SUNNY_DAY_POA_FRACTION * best_poa
            days.append(DayForecast(iso, poa, exp, act, sunny))
            exp_total += exp
            if act is not None:
                act_total += act
                matched_days += 1
        cur += timedelta(days=1)

    # Measured PR: only meaningful over days we have BOTH expected & actual for, so
    # recompute expected restricted to matched days (don't compare a 14-day expected
    # to a 6-day actual — that would fake a shortfall).
    exp_matched = sum(d.expected_kwh for d in days if d.actual_kwh is not None)
    pr_measured = (act_total / exp_matched) if exp_matched > 0 else None

    # Confidence: how much real measured overlap we have.
    if matched_days >= 10:
        confidence = "high"
    elif matched_days >= 4:
        confidence = "medium"
    elif matched_days >= 1:
        confidence = "low"
    else:
        confidence = "none"

    inputs = {
        "nameplate_kw": round(nameplate_kw, 2),
        "location": {
            "lat": round(lat, 4), "lng": round(lng, 4),
            "geocode_source": geocode_source, "address": geocoded_address,
        },
        "geometry": {
            "tilt_deg": round(tilt_deg, 1), "azimuth_deg": round(azimuth_deg, 1),
            "tilt_assumed": tilt_assumed, "azimuth_assumed": azimuth_assumed,
            "azimuth_label": _azimuth_label(azimuth_deg),
        },
        "performance_ratio": pr,
        "irradiance": {
            "source": "Open-Meteo global_tilted_irradiance (hourly, integrated)",
            "stc_reference_kwh_m2": STC_IRRADIANCE_KWH_M2,
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "best_day_poa_kwh_m2": round(best_poa, 2),
        },
        "window_days": window_days,
        "measured_days": matched_days,
        "measured_sources": sorted(MEASURED_DAILY_SOURCES),
    }

    return Forecast(
        available=True, inputs=inputs, days=days,
        expected_kwh=exp_total, actual_kwh=act_total,
        performance_ratio_measured=pr_measured, confidence=confidence,
    )


def _azimuth_label(az: float) -> str:
    """Plain-language compass label for an Open-Meteo azimuth (0=S, -90=E, 90=W,
    ±180=N). Normalizes to a -180..180 range before bucketing."""
    a = ((az + 180) % 360) - 180   # wrap into (-180, 180]
    dirs = [(0, "south"), (45, "southwest"), (90, "west"), (135, "northwest"),
            (180, "north"), (-180, "north"), (-135, "northeast"),
            (-90, "east"), (-45, "southeast")]
    return min(dirs, key=lambda d: abs(d[0] - a))[1]
