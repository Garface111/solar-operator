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
    return bool(config.SHEETS_ID and config.SHEETS_SERVICE_ACCOUNT_JSON)


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
