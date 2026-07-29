"""The household's own planning spreadsheet, as something the copilot can read.

Ford and Gaurav keep a daily cash-flow ledger in Google Sheets: one row per date,
with each spouse's running balance, money in and out, cash on hand, stocks, and
credit-card projections. It is the model they actually plan against, so the
copilot should read it rather than invent a parallel view — and it can reconcile
its own bank data against what the sheet claims.

READING needs no credentials while the sheet is link-shared: Google serves a CSV
export of any viewable sheet. WRITING is a different matter and needs a Google
service account (see SHEETS_SETUP in the README) — the write path stays dormant
until SHEETS_SERVICE_ACCOUNT_JSON exists, exactly like every other integration
here.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date

import httpx

from .. import config

CSV_EXPORT = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
#: gid=0 is NOT reliably the tab people see first — it is the original sheet id,
#: and a workbook whose first tab was added later exports something else
#: entirely. Omitting gid gives the default tab, which is what they opened.

#: Column headings that hold money, mapped to the shorter names the copilot uses.
MONEY_COLUMNS = {
    "Gaurav End Running Balance": "gaurav_balance",
    "Gaurav In": "gaurav_in",
    "Gaurav Out": "gaurav_out",
    "Ford Running Balance": "ford_balance",
    "Ford In": "ford_in",
    "Ford Out": "ford_out",
    "Total Cash In Hand": "total_cash",
    "Stocks": "stocks",
    "Apple Card Payment": "apple_card_payment",
    "Apple Card Projection": "apple_card_projection",
    "Other CC": "other_cc",
    "Excess": "excess",
}


def configured() -> bool:
    return bool(config.SHEETS_ID)


def can_write() -> bool:
    """Two ways in, and the simpler one is usually the only one available.

    Google's "Secure by Default" org policy (iam.disableServiceAccountKeyCreation)
    blocks downloading service-account keys on most new organisations, and turning
    that policy off to let a household tool write a spreadsheet is a bad trade. An
    Apps Script web app bound to the sheet needs no key and no IAM change: the
    script runs as its owner, so it already has access, and a shared secret gates
    the endpoint.
    """
    return bool(config.SHEETS_ID) and bool(
        config.SHEETS_WEBHOOK_URL or config.SHEETS_SERVICE_ACCOUNT_JSON
    )


def _money(raw: str) -> float | None:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def _parse_date(raw: str) -> date | None:
    text = (raw or "").strip()
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if match:
        month, day, year = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def fetch_csv(gid: str | None = None) -> str:
    if not config.SHEETS_ID:
        raise RuntimeError("SHEETS_ID is not set in .env")
    url = CSV_EXPORT.format(sheet_id=config.SHEETS_ID)
    tab = gid or config.SHEETS_GID
    if tab:
        url += f"&gid={tab}"
    resp = httpx.get(url, timeout=60, follow_redirects=True)
    if resp.status_code == 403 or "signin" in str(resp.url):
        raise RuntimeError(
            "the spreadsheet is not readable — set link sharing to 'Anyone with "
            "the link can view', or configure a service account"
        )
    resp.raise_for_status()
    return resp.text


def parse(csv_text: str) -> dict:
    """Rows keyed by date, plus the headings we did not recognise.

    Unknown columns are reported rather than dropped: this is the household's own
    sheet and they will add columns without telling anyone.
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return {"rows": [], "headers": [], "unmapped_columns": []}
    headers = [h.strip() for h in rows[0]]
    index = {h: i for i, h in enumerate(headers) if h}
    parsed: list[dict] = []
    for raw in rows[1:]:
        if not raw or not raw[0].strip():
            continue
        day = _parse_date(raw[0])
        if day is None:
            continue
        entry: dict = {"date": day.isoformat()}
        for heading, key in MONEY_COLUMNS.items():
            position = index.get(heading)
            if position is not None and position < len(raw):
                entry[key] = _money(raw[position])
        for label in ("Expense", "Type"):
            position = index.get(label)
            if position is not None and position < len(raw) and raw[position].strip():
                entry[label.lower()] = raw[position].strip()
        parsed.append(entry)
    known = set(MONEY_COLUMNS) | {"Date", "Expense", "Type", "Off by", "AC Off 2", "Excess 2"}
    unmapped = [h for h in headers if h and h not in known]
    return {"rows": parsed, "headers": [h for h in headers if h], "unmapped_columns": unmapped}


def read_plan(limit_days: int = 45) -> dict:
    """The most recent rows of the plan, newest last."""
    data = parse(fetch_csv())
    rows = data["rows"]
    today = date.today().isoformat()
    past = [r for r in rows if r["date"] <= today]
    future = [r for r in rows if r["date"] > today]
    return {
        "sheet_id": config.SHEETS_ID,
        "total_rows": len(rows),
        "columns": data["headers"],
        "unmapped_columns": data["unmapped_columns"],
        "recent": past[-limit_days:],
        "upcoming": future[:limit_days],
        "writable": can_write(),
        "note": (
            "This is the household's own planning sheet, read live. It is THEIR "
            "model — reconcile against it and point out disagreements with the "
            "bank data rather than overwriting their intent."
            + ("" if can_write() else " Writing is not configured yet, so any "
               "correction has to be described to them, not applied.")
        ),
    }


SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

#: The copilot writes HERE and nowhere else. Their workbook is full of formulas
#: (running balances, "Off by", "Excess") whose computed values are all a CSV
#: export shows — writing a number into a formula cell would silently destroy the
#: formula and there is no undo an agent can perform. Instead it maintains one
#: plain data tab that their own formulas can reference, so their model stays
#: theirs and every value the copilot produced is visible in one place.
ACTUALS_TAB = "BankAI Actuals"


def _credentials():
    from google.oauth2 import service_account

    path = config.SHEETS_SERVICE_ACCOUNT_JSON
    if not path:
        raise RuntimeError(
            "SHEETS_SERVICE_ACCOUNT_JSON is not set — download the service "
            "account's JSON key and point this at it"
        )
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


def _token() -> str:
    import google.auth.transport.requests

    creds = _credentials()
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def service_account_email() -> str | None:
    """Who the sheet must be shared with, read from the key itself."""
    import json

    path = config.SHEETS_SERVICE_ACCOUNT_JSON
    if not path:
        return None
    try:
        with open(path) as handle:
            return json.load(handle).get("client_email")
    except Exception:
        return None


def _api(method: str, path: str, **kwargs) -> dict:
    url = SHEETS_API.format(sheet_id=config.SHEETS_ID) + path
    headers = {"Authorization": f"Bearer {_token()}"}
    resp = httpx.request(method, url, headers=headers, timeout=60, **kwargs)
    if resp.status_code == 403:
        who = service_account_email() or "the service account"
        raise RuntimeError(
            f"Google refused the write. Share the spreadsheet with {who} and give "
            "it Editor access (Share → paste the address → Editor)."
        )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def tabs() -> list[str]:
    data = _api("GET", "?fields=sheets.properties.title")
    return [s["properties"]["title"] for s in data.get("sheets", [])]


def ensure_actuals_tab() -> None:
    if ACTUALS_TAB in tabs():
        return
    _api("POST", ":batchUpdate", json={
        "requests": [{"addSheet": {"properties": {"title": ACTUALS_TAB}}}]
    })


def _write_via_apps_script(tab: str, values: list[list]) -> dict:
    """Hand the rows to a script that already owns the sheet."""
    resp = httpx.post(
        config.SHEETS_WEBHOOK_URL,
        json={"secret": config.SHEETS_WEBHOOK_SECRET, "tab": tab, "values": values},
        timeout=60,
        follow_redirects=True,  # Apps Script /exec redirects to googleusercontent
    )
    resp.raise_for_status()
    body = resp.text.strip()
    if "unauthorized" in body.lower():
        raise RuntimeError(
            "the Apps Script rejected the secret — SHEETS_WEBHOOK_SECRET must match "
            "the SECRET constant in the script"
        )
    if body.startswith("<!DOCTYPE") or "<html" in body[:200].lower():
        raise RuntimeError(
            "the Apps Script URL returned a login page — redeploy it with "
            "'Execute as: Me' and 'Who has access: Anyone'"
        )
    return {"transport": "apps_script", "response": body[:300]}


def write_rows(tab: str, values: list[list]) -> dict:
    """Write a block of rows, using whichever transport is configured."""
    if config.SHEETS_WEBHOOK_URL:
        return _write_via_apps_script(tab, values)
    ensure_actuals_tab()
    _api(
        "PUT",
        f"/values/{tab}!A1:C{len(values) + 5}",
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": values},
    )
    return {"transport": "service_account"}


def write_actuals(session) -> dict:
    """Publish today's real figures into the copilot's own tab.

    Deliberately a full rewrite of a small block rather than an append: re-running
    it is idempotent, and there is never a growing pile of half-stale rows.
    """
    from datetime import datetime

    from ..intelligence.insights import net_worth

    if not can_write():
        raise RuntimeError(
            "writing is not configured — set SHEETS_WEBHOOK_URL (the Apps Script "
            "web app bound to the sheet) or SHEETS_SERVICE_ACCOUNT_JSON"
        )

    live = net_worth(session)
    accounts = live["accounts"]

    def total(*kinds: str) -> float:
        return round(sum(
            a["balance"] for a in accounts
            if a["kind"] in kinds and a["balance"] is not None
        ), 2)

    stamp = datetime.utcnow().isoformat(timespec="seconds")
    rows: list[list] = [
        ["Written by the household copilot — do not edit by hand", "", ""],
        ["Last updated (UTC)", stamp, ""],
        ["", "", ""],
        ["Figure", "Amount", "Source"],
        ["Cash (checking + savings)", total("checking", "savings"), "bank feed"],
        ["Investments", total("investment"), "bank feed"],
        ["Credit cards owed", total("credit"), "bank feed"],
        ["Property", total("property"), "comps tracker"],
        ["Mortgage + loans", total("mortgage", "loan"), "statement / manual"],
        ["NET WORTH", live["total"], "sum of the above"],
        ["", "", ""],
        ["Account", "Balance", "Kind"],
    ]
    for a in sorted(accounts, key=lambda x: x["name"]):
        rows.append([a["name"], a["balance"], a["kind"]])

    result = write_rows(ACTUALS_TAB, rows)
    return {
        "written": True,
        "tab": ACTUALS_TAB,
        "rows": len(rows),
        "net_worth": live["total"],
        "transport": result.get("transport"),
        "note": (
            "Written to a dedicated tab, not into their planning columns — their "
            "formulas are untouched and can reference these cells."
        ),
    }


def reconcile(session, limit_days: int = 14) -> dict:
    """Compare what the sheet claims against what the accounts actually hold."""
    from ..intelligence.insights import net_worth

    plan = read_plan(limit_days=limit_days)
    recent = [r for r in plan["recent"] if r.get("total_cash") is not None]
    latest = recent[-1] if recent else None

    live = net_worth(session)
    liquid = sum(
        a["balance"] for a in live["accounts"]
        if a["kind"] in ("checking", "savings") and a["balance"] is not None
    )
    out = {
        "sheet_latest_row": latest,
        "live_liquid_total": round(liquid, 2),
        "live_net_worth": live["total"],
    }
    if latest and latest.get("total_cash") is not None:
        difference = round(liquid - latest["total_cash"], 2)
        out["difference_vs_sheet_cash"] = difference
        out["interpretation"] = (
            "sheet and accounts agree within $50" if abs(difference) < 50
            else "sheet and accounts disagree — say by how much and on which date, "
                 "and ask which is right before changing anything"
        )
    return out
