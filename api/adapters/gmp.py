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
    }
