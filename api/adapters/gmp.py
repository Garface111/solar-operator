"""
Provider adapter for Green Mountain Power.

Two responsibilities:
  1. Given a captured payload from the Chrome extension, normalize it.
  2. Given a stored session (JWT + account meta), pull the latest bill PDF
     and extract kWh + billing days.

The bill-fetch flow is the one we reverse-engineered with Ford:
  GET <currentBillUrl>              -> HTML form (with AntiForgery token)
  POST https://document.utilitec.net/Webview <form fields>  -> PDF bytes
"""
from __future__ import annotations
import re, html as htmllib, urllib.parse, pathlib
from datetime import datetime
from typing import Any
import httpx, pdfplumber

PROVIDER = "gmp"


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
                "nickname":        a.get("nickname"),
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
