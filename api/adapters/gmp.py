"""
Provider adapter for Green Mountain Power.

Two responsibilities:
  1. Given a captured payload from the Chrome extension, normalize it.
  2. Given a stored session (JWT + account meta), pull bill data and
     extract kWh + billing days.

Two bill-fetch strategies, in preference order:

  A) JSON API (gold standard, preferred):
     GET https://api.greenmountainpower.com/api/v2/accounts/{acct}/bills
       Authorization: Bearer <JWT>
     → JSON with full history; KWH GENERATE line item per bill segment.
     No regex, no PDF parsing — just numbers.

  B) PDF redirector (fallback if JSON fails):
     GET <currentBillUrl>              -> HTML form (with AntiForgery token)
     POST https://document.utilitec.net/Webview <form fields>  -> PDF bytes
     Parse with pdfplumber + regex.
"""
from __future__ import annotations
import re, html as htmllib, urllib.parse, pathlib
from datetime import datetime
from typing import Any
import httpx, pdfplumber
from ._gmp_clean import clean_gmp_nickname

PROVIDER = "gmp"
GMP_API_BASE = "https://api.greenmountainpower.com"


# ─── Daily 15-minute interval USAGE CSV (the multi-year DATA SPONGE) ──────────
# Grounded on a live read-only probe (2026-06-18), documented in
# docs/plans/GMP_DAILY_READ_CONTRACT.md:
#   GET /api/v2/usage/{acct}/download?startDate=&endDate=&format=csv  (Bearer JWT)
#   → 15-min interval CSV, cols: ServiceAgreement, IntervalStart, IntervalEnd,
#     Quantity, UnitOfMeasure(kWh). Below a meter's history floor GMP returns a
#     clean 404; a ~1-year request 503-times-out server-side (page in <=90d).
# The backfill job (api/jobs/gmp_daily_backfill.py) depends on these.

class GmpUsageNotFound(Exception):
    """GMP returned 404 for a usage window — below the meter's history floor."""


class GmpUsageTimeout(Exception):
    """GMP 503/timeout — the requested window is too large; caller should shrink."""


def fetch_usage_csv(account_number: str, jwt: str, start, end, timeout: float = 60.0) -> str:
    """Fetch the 15-minute interval generation CSV for one GMP meter over
    [start, end). `start`/`end` are date or datetime.

    Raises:
      GmpUsageNotFound  on HTTP 404 (below the meter's earliest data),
      GmpUsageTimeout   on HTTP 503 / read timeout (window too big),
      ValueError        on any other non-200 (e.g. 401/403 expired JWT — the
                        status code is embedded in the message for the caller's
                        refresh-retry logic).
    """
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "text/csv, application/json, text/plain, */*",
        "Origin": "https://greenmountainpower.com",
        "Referer": "https://greenmountainpower.com/",
        "GMP-Source": "web",
        "User-Agent": "Mozilla/5.0 (Solar Operator)",
    }
    sd = start.strftime("%Y-%m-%d") if hasattr(start, "strftime") else str(start)
    ed = end.strftime("%Y-%m-%d") if hasattr(end, "strftime") else str(end)
    url = f"{GMP_API_BASE}/api/v2/usage/{account_number}/download"
    params = {"startDate": sd, "endDate": ed, "format": "csv"}
    try:
        with httpx.Client(timeout=timeout, headers=headers) as c:
            r = c.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise GmpUsageTimeout(f"GMP usage CSV timed out for {sd}..{ed}") from exc
    if r.status_code == 404:
        raise GmpUsageNotFound(f"GMP usage 404 for {account_number} {sd}..{ed}")
    if r.status_code == 503:
        raise GmpUsageTimeout(f"GMP usage 503 for {account_number} {sd}..{ed}")
    if r.status_code != 200:
        raise ValueError(f"GMP usage CSV returned HTTP {r.status_code}")
    return r.text


def parse_usage_csv_to_daily(csv_text: str) -> dict[str, Any]:
    """Parse a GMP 15-minute interval CSV into per-day kWh aggregates.

    Columns (case-insensitive, order-tolerant): ServiceAgreement, IntervalStart,
    IntervalEnd, Quantity, UnitOfMeasure. The day is taken from IntervalStart.
    kWh per day = Σ Quantity for that day; intervals = count of rows that day.

    NEVER fabricates: a blank/missing/unparseable Quantity contributes nothing.
    Negative interval noise is preserved here (the modeled layer clamps to >=0).

    Returns:
        {
          "by_day": {date: {"kwh": float, "intervals": int}},
          "row_count": int,                 # data rows parsed
          "interval_min": date|None,        # earliest day seen
          "interval_max": date|None,        # latest day seen
          "service_agreements": [str],      # distinct SA ids present
          "unit": str|None,                 # UnitOfMeasure (expect 'kWh')
        }
    """
    import csv as _csv
    import io as _io

    empty = {"by_day": {}, "row_count": 0, "interval_min": None,
             "interval_max": None, "service_agreements": [], "unit": None}
    if not csv_text or not csv_text.strip():
        return empty

    reader = _csv.reader(_io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return empty

    # Locate the header row (GMP sometimes prepends a title line); find the row
    # that contains an "interval" + "quantity" column.
    header_idx = None
    for i, row in enumerate(rows[:5]):
        low = [c.strip().lower() for c in row]
        if any("interval" in c or c == "date" for c in low) and any("quantity" in c or "kwh" in c for c in low):
            header_idx = i
            break
    if header_idx is None:
        header_idx = 0
    header = [c.strip().lower() for c in rows[header_idx]]

    def col(*names):
        for nm in names:
            for j, h in enumerate(header):
                if h == nm or nm in h:
                    return j
        return None

    i_start = col("intervalstart", "interval start", "start", "date")
    i_qty = col("quantity", "kwh", "usage", "value")
    i_sa = col("serviceagreement", "service agreement", "account", "meter")
    i_unit = col("unitofmeasure", "unit of measure", "unit", "uom")
    if i_start is None or i_qty is None:
        return empty

    by_day: dict = {}
    row_count = 0
    sas: set = set()
    unit = None
    dmin = dmax = None

    for row in rows[header_idx + 1:]:
        if not row or len(row) <= max(i_start, i_qty):
            continue
        raw_dt = (row[i_start] or "").strip().strip('"')
        if not raw_dt:
            continue
        d = _parse_interval_date(raw_dt)
        if d is None:
            continue
        qty = _to_float((row[i_qty] or "").strip().strip('"').replace(",", ""))
        if qty is None:
            continue  # never fabricate — a missing reading is skipped
        cell = by_day.setdefault(d, {"kwh": 0.0, "intervals": 0})
        cell["kwh"] += qty
        cell["intervals"] += 1
        row_count += 1
        if i_sa is not None and i_sa < len(row):
            sa = (row[i_sa] or "").strip()
            if sa:
                sas.add(sa)
        if unit is None and i_unit is not None and i_unit < len(row):
            u = (row[i_unit] or "").strip()
            if u:
                unit = u
        dmin = d if dmin is None else min(dmin, d)
        dmax = d if dmax is None else max(dmax, d)

    # round day totals to avoid float dust
    for d in by_day:
        by_day[d]["kwh"] = round(by_day[d]["kwh"], 6)

    return {
        "by_day": by_day,
        "row_count": row_count,
        "interval_min": dmin,
        "interval_max": dmax,
        "service_agreements": sorted(sas),
        "unit": unit,
    }


def _parse_interval_date(s: str):
    """Extract the calendar date from a GMP IntervalStart cell. Handles
    'YYYY-MM-DD HH:MM:SS', ISO 'YYYY-MM-DDTHH:MM:SS', and 'MM/DD/YYYY HH:MM'."""
    s = s.strip()
    # take just the date portion before any space or 'T'
    head = s.split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    # last resort: full ISO parse
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def parse_extension_payload(payload: dict) -> dict:
    """Normalize the Chrome extension's POST body into structured fields the
    API can persist directly."""
    return {
        "provider": payload.get("provider", PROVIDER),
        "captured_at": payload.get("capturedAt"),
        "user": payload.get("user", {}),
        "auth": payload.get("auth", {}),
        "accounts": [
            {
                "account_number":  a.get("accountNumber"),
                "customer_number": a.get("customerNumber"),
                "nickname":        clean_gmp_nickname(a.get("nickname")),
                "current_bill_url": a.get("currentBillUrl"),
                "service_address": a.get("serviceAddress"),
                "extra": {
                    "isPrimary":         a.get("isPrimary"),
                    "solarNetMeter":     a.get("solarNetMeter"),
                    "groupNetMetered":   a.get("groupNetMetered"),
                    "currentBillUrlBinary": a.get("currentBillUrlBinary"),
                },
            }
            for a in payload.get("accounts", [])
        ],
    }


_INPUT_RE = re.compile(r'<input[^>]*?name="([^"]+)"[^>]*?value="([^"]*)"')


def fetch_bill_pdf(current_bill_url: str, out_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    """Fetch the bill PDF for one account. Returns (path, content_type).

    Raises httpx.HTTPError on transport failures and ValueError on form-parse
    failures (provider HTML changed).
    """
    headers = {"User-Agent": "Mozilla/5.0 (Solar Operator)"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        r = client.get(current_bill_url)
        r.raise_for_status()
        inputs = _INPUT_RE.findall(r.text)
        if not inputs:
            raise ValueError(f"No form fields in redirector HTML ({len(r.text)} bytes)")
        form = {n: htmllib.unescape(v) for n, v in inputs}
        body = urllib.parse.urlencode(form)
        r2 = client.post(
            "https://document.utilitec.net/Webview",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://document.utilitec.net/",
            },
        )
        r2.raise_for_status()
        out_path.write_bytes(r2.content)
        return out_path, r2.headers.get("content-type", "")


_KWH_RE = re.compile(r'Total(?:\s+Gross)?\s+([\d,]+)\s+KWH\s+Generated', re.I)
_PERIOD_RE = re.compile(r'Usage Period:\s*(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})')
_BILLDATE_RE = re.compile(r'Bill Date\s+(\d{2}/\d{2}/\d{2})')
_DOC_RE = re.compile(r'Account Number\s+(\d{11,})')


def extract_bill_metrics(pdf_path: pathlib.Path) -> dict[str, Any]:
    """Pull kWh + billing window from a parsed GMP bill PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    m_gen = _KWH_RE.search(text)
    m_per = _PERIOD_RE.search(text)
    m_bd  = _BILLDATE_RE.search(text)

    period_start = period_end = None
    days = None
    if m_per:
        period_start = datetime.strptime(m_per.group(1), "%m/%d/%y")
        period_end   = datetime.strptime(m_per.group(2), "%m/%d/%y")
        days = (period_end - period_start).days

    bill_date = datetime.strptime(m_bd.group(1), "%m/%d/%y") if m_bd else None
    kwh = int(m_gen.group(1).replace(",", "")) if m_gen else None

    status = "parsed" if (kwh is not None and days is not None) else "partial"
    return {
        "kwh_generated": kwh,
        "period_start":  period_start,
        "period_end":    period_end,
        "billing_days":  days,
        "bill_date":     bill_date,
        "raw_text":      text,
        "parse_status":  status,
    }


# ─── JSON API (preferred path) ──────────────────────────────────────────

def fetch_bills_json(account_number: str, jwt: str, timeout: int = 30) -> list[dict]:
    """GET full bill history for one account.

    Raises httpx.HTTPError on transport failures and ValueError if GMP
    returns a non-200 (typically expired JWT)."""
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://greenmountainpower.com",
        "Referer": "https://greenmountainpower.com/",
        "GMP-Source": "web",
        "User-Agent": "Mozilla/5.0 (Solar Operator)",
    }
    url = f"{GMP_API_BASE}/api/v2/accounts/{account_number}/bills"
    with httpx.Client(timeout=timeout, headers=headers) as c:
        r = c.get(url)
    if r.status_code != 200:
        raise ValueError(f"GMP JSON API returned HTTP {r.status_code}")
    return r.json()


def _extract_kwh_generated(bill: dict) -> float | None:
    """Largest non-zero KWH GENERATE line item across all segments.

    Each bill has a placeholder 0.0 GENERATE row plus the real total; some
    have duplicates (generation + solar incentive credit) with identical
    values — max collapses safely."""
    best = 0.0
    for seg in bill.get("billSegments", []):
        for li in seg.get("segmentLineItems", []):
            if (li.get("unitOfMeasure") == "KWH"
                    and li.get("unitCode") == "GENERATE"):
                v = float(li.get("unitCount") or 0)
                if v > best:
                    best = v
    return best if best > 0 else None


# GMP bill JSON line-item unitCodes — VERIFIED against live prod raw_json
# (introspected 2026-06-18 across 400+ real bills). The energy record lives in
# segmentLineItems[].unitCode (KWH) + dollarAmount; the bill's money is in
# segmentCalcs[].dollarAmount (rate-level summary, sums to the same total as the
# line items — use ONE, never both). raw_json is still stored so any field not
# modeled here is recoverable without a re-pull.
_CONSUME_CODES = {"CONSUMED"}              # KWH consumed from the grid
_SENT_CODES = {"EXCESS", "EXCESSO"}        # KWH excess generation sent to grid (credited)
_NET_CODES = {"NET"}                       # net KWH (consumed - generated)
_SOLCRED_CODES = {"SOLCRED"}               # solar credit KWH line(s)


def _sum_kwh_by_codes(bill: dict, codes: set[str]) -> float | None:
    """Largest single KWH line item per code-set across all segments. GMP repeats
    a placeholder 0.0 row plus the real total (and sometimes duplicates), so MAX
    (not SUM) collapses safely — mirrors _extract_kwh_generated."""
    best = None
    for seg in bill.get("billSegments", []):
        for li in seg.get("segmentLineItems", []):
            if li.get("unitOfMeasure") == "KWH" and li.get("unitCode") in codes:
                v = _to_float(li.get("unitCount"))
                if v is not None and (best is None or abs(v) > abs(best)):
                    best = v
    return best


def _bill_total_cost(bill: dict) -> float | None:
    """Total $ for the bill = sum of segmentCalcs[].dollarAmount (the rate-level
    charge summary). Negative = a net credit (net-metering customer earning).
    Falls back to summing line-item dollarAmounts if no segmentCalcs exist."""
    calc_total = 0.0
    calc_found = False
    li_total = 0.0
    li_found = False
    for seg in bill.get("billSegments", []):
        for c in (seg.get("segmentCalcs") or []):
            d = _to_float(c.get("dollarAmount"))
            if d is not None:
                calc_total += d
                calc_found = True
        for li in seg.get("segmentLineItems", []):
            d = _to_float(li.get("dollarAmount"))
            if d is not None:
                li_total += d
                li_found = True
    if calc_found:
        return round(calc_total, 2)
    if li_found:
        return round(li_total, 2)
    return None


def _extract_full_record(bill: dict) -> dict[str, Any]:
    """Extract the FULL energy record from a GMP bill JSON, grounded on the REAL
    field names verified against live prod data: consumption (CONSUMED), excess
    sent to grid (EXCESS), gross generation (GENERATE), bill cost (segmentCalcs
    dollarAmount — negative = credit), blended rate, net-metered flag.

    raw_json is stored alongside so anything not modeled here is recoverable."""
    gross = _extract_kwh_generated(bill)
    consumed = _sum_kwh_by_codes(bill, _CONSUME_CODES)
    sent = _sum_kwh_by_codes(bill, _SENT_CODES)

    total_cost = _bill_total_cost(bill)
    # Net-metering credit: a NEGATIVE bill total IS the credit the owner earned.
    net_credit = -total_cost if (total_cost is not None and total_cost < 0) else None

    # Gross SOLAR credit (EXCESS + SOLCRED) the array earned — the OFFTAKER billing
    # basis. None for banked months (excess credited at ~$0) so the offtaker invoice
    # skips them instead of over-charging from gross kWh × a flat rate.
    from ..rate_schedule import solar_credit_from_bill
    _sc = solar_credit_from_bill(bill)
    solar_credit_usd = _sc["credit_usd"] if _sc else None

    # Blended rate (¢/kWh): |cost| / kWh consumed, only when both meaningful.
    # Guard the divide-by-tiny artifact: a solar bill with ~0 consumption but
    # fixed charges produces an absurd rate (e.g. $29 / 2 kWh = 1456¢). Require a
    # real consumption floor and clamp to a sane utility ceiling (~100¢/kWh).
    avg_rate = None
    if total_cost is not None and consumed and consumed >= 10:
        r = round((abs(total_cost) / consumed) * 100.0, 3)
        if 0 <= r <= 100:
            avg_rate = r

    # Net-metered if the bill carries any excess/solar-credit/generation signal.
    is_nm = None
    codes_seen = {
        li.get("unitCode")
        for seg in bill.get("billSegments", [])
        for li in seg.get("segmentLineItems", [])
    }
    if codes_seen:
        nm_codes = _SENT_CODES | _SOLCRED_CODES | {"GENERATE"}
        is_nm = bool((codes_seen & nm_codes) and (gross or sent))

    return {
        "kwh_gross_generated": gross,
        "kwh_consumed_full": consumed,
        "kwh_sent_to_grid": sent,
        "total_cost": total_cost,
        "net_credit": net_credit,
        "solar_credit_usd": solar_credit_usd,
        "avg_rate_cents_kwh": avg_rate,
        "supplier": "Green Mountain Power",
        "is_net_metered": is_nm,
    }


def _segment_dates(bill: dict) -> tuple[datetime | None, datetime | None]:
    """First segment's (startDate, endDate) as datetimes, or (None, None)."""
    for seg in bill.get("billSegments", []):
        sc = (seg.get("segmentCalcs") or [{}])[0]
        sd, ed = sc.get("startDate"), sc.get("endDate")
        try:
            sd_dt = datetime.fromisoformat(sd) if sd else None
        except Exception:
            sd_dt = None
        try:
            ed_dt = datetime.fromisoformat(ed) if ed else None
        except Exception:
            ed_dt = None
        if sd_dt or ed_dt:
            return sd_dt, ed_dt
    return None, None


def _to_float(v: Any) -> float | None:
    """Coerce a JSON number/string into a float, or None if not parseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_usage_summary(summary: dict) -> dict[str, Any]:
    """Normalize a GMP /usage/{acct}/summary body into the generation signal we
    persist for Array Operator (kWh the array produced / sent to grid).

    Defensive about missing keys — GMP omits or nulls fields for accounts with no
    solar (isNetMetered=false), so every numeric read is coerced and may be None.

    Returns:
        account_number   — str | None
        is_net_metered   — bool (truthy => the meter participates in net metering)
        period_start     — ISO str | None (billingPeriodStartDate)
        period_end       — ISO str | None (billingPeriodEndDate)
        kwh_generated    — float | None  (totalGrossGenerated; falls back to
                           totalGenerationSentToGrid when gross is 0/None)
        kwh_sent_to_grid — float | None  (totalGenerationSentToGrid)
        kwh_consumed     — float | None  (totalConsumption)
    """
    summary = summary or {}

    gross = _to_float(summary.get("totalGrossGenerated"))
    sent = _to_float(summary.get("totalGenerationSentToGrid"))

    # Prefer gross generation (what the array actually produced). For a
    # net-metered account that under-reports gross (0/None) we fall back to the
    # energy exported to the grid so we never lose a real solar signal.
    if gross is not None and gross > 0:
        kwh_generated = gross
    elif sent is not None and sent > 0:
        kwh_generated = sent
    else:
        # Both zero/None — keep gross's value (0.0 or None) so the caller can
        # honestly distinguish "no solar" from "missing data".
        kwh_generated = gross if gross is not None else sent

    return {
        "account_number": summary.get("accountNumber"),
        "is_net_metered": bool(summary.get("isNetMetered")),
        "period_start": summary.get("billingPeriodStartDate"),
        "period_end": summary.get("billingPeriodEndDate"),
        "kwh_generated": kwh_generated,
        "kwh_sent_to_grid": sent,
        "kwh_consumed": _to_float(summary.get("totalConsumption")),
    }


def bill_json_to_metrics(bill: dict) -> dict[str, Any]:
    """Convert a single bill JSON entry into the same metrics dict shape as
    extract_bill_metrics() so worker.py can persist either uniformly."""
    kwh = _extract_kwh_generated(bill)

    bd_str = bill.get("billDate")
    try:
        bill_date = datetime.fromisoformat(bd_str) if bd_str else None
    except Exception:
        bill_date = None

    period_start, period_end = _segment_dates(bill)
    days = (period_end - period_start).days if (period_start and period_end) else None

    status = "parsed" if (kwh is not None and days is not None) else "partial"
    full = _extract_full_record(bill)
    return {
        "kwh_generated": int(round(kwh)) if kwh is not None else None,
        "period_start":  period_start,
        "period_end":    period_end,
        "billing_days":  days,
        "bill_date":     bill_date,
        "raw_text":      "",  # not applicable for JSON path
        "parse_status":  status,
        "source":        "json",
        "document_number": bill.get("billNumber") or bill.get("invoiceNumber"),
        # ── full energy record (the sponge) ──────────────────────────────────
        "kwh_consumed":        int(round(full["kwh_consumed_full"])) if full["kwh_consumed_full"] is not None else None,
        "kwh_sent_to_grid":    full["kwh_sent_to_grid"],
        "kwh_gross_generated": full["kwh_gross_generated"],
        "is_net_metered":      full["is_net_metered"],
        "total_cost":          full["total_cost"],
        "net_credit":          full["net_credit"],
        "solar_credit_usd":    full["solar_credit_usd"],
        "avg_rate_cents_kwh":  full["avg_rate_cents_kwh"],
        "supplier":            full["supplier"],
        "raw_json":            bill,   # the authoritative full record — never lose a field
    }
