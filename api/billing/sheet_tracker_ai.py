"""AI column-mapper for the BYO generation-spreadsheet tracker.

The deterministic heuristic in sheet_tracker.py handles common layouts offline.
This module is the "intelligence" upgrade Ford asked for: when an ANTHROPIC_API_KEY
is present on the box, we hand the model the first rows of the uploaded sheet and let
it map the logical fields to columns — so an arbitrary layout (multiple kWh columns,
split tariff/adder, a header buried under sub-headers, the offtaker's own named column)
is read correctly without per-customer tuning.

Design rules honored:
  * BEST-EFFORT + isolated: any failure (no key, network, bad JSON, low confidence)
    returns None, and the caller falls back to the heuristic. It can NEVER break upload.
  * Minimal data leaves the box: only the first ~20 rows × ~30 cols (the header region
    + a few sample rows), not the whole ledger.
  * The result is still surfaced to the operator (mapping chips + a remap endpoint),
    so a wrong guess is correctable — the model assists, it isn't trusted blindly.
  * No SDK dependency: a plain HTTPS POST via urllib, so nothing new to install.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FIELDS = ("period", "generation", "consumption", "rate", "amount")
_API_URL = "https://api.anthropic.com/v1/messages"
_MAX_ROWS = 20
_MAX_COLS = 30
_MIN_CONFIDENCE = 0.45


def ai_available() -> bool:
    """True when the model-mapper can run (a key is configured + not disabled)."""
    if os.getenv("SHEET_TRACKER_AI", "").lower() in ("0", "false", "off", "no"):
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _model() -> str:
    # Fast + cheap is plenty for column mapping; the operator confirms the result.
    # Bump via SHEET_TRACKER_AI_MODEL (e.g. claude-sonnet-4-6, claude-opus-4-8).
    return os.getenv("SHEET_TRACKER_AI_MODEL", "claude-haiku-4-5-20251001")


def _trunc(v: Any, n: int = 24) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\n", " ").strip()
    return s[:n]


def _render_grid(grid: list[list]) -> tuple[str, int]:
    """A compact, indexed rendering of the top of the sheet for the prompt.
    Returns (text, width) where width is the max column count seen."""
    width = 0
    lines = []
    for ridx, row in enumerate(grid[:_MAX_ROWS]):
        cells = list(row)[:_MAX_COLS]
        width = max(width, len(cells))
        rendered = " | ".join(f"[{ci}] {_trunc(c)}" for ci, c in enumerate(cells))
        lines.append(f"row {ridx}: {rendered}")
    return "\n".join(lines), width


def _prompt(grid_text: str, sheet: Optional[str], offtaker: Optional[str]) -> str:
    who = f'\nThe offtaker (the customer this sheet bills) is named: "{offtaker}".' if offtaker else ""
    sh = f' (sheet "{sheet}")' if sheet else ""
    return (
        "You map the columns of a solar generation-tracking spreadsheet so an automated "
        "system can append one new row per billing month. Below are the first rows of an "
        f"uploaded sheet{sh}; columns are 0-indexed (shown as [n]).{who}\n\n"
        f"{grid_text}\n\n"
        "Identify the single column index for each field (or null if absent):\n"
        "- period: the billing month / date column (one entry per billing period).\n"
        "- generation: the offtaker's MONTHLY generation/production in kWh that their bill "
        "is computed from. If the sheet has BOTH a whole-array column and a per-offtaker "
        "share (often named after the offtaker), choose the OFFTAKER's share. NEVER choose a "
        "cumulative / running-total / year-to-date column.\n"
        "- consumption: monthly consumption/usage kWh, if present (often absent).\n"
        "- rate: the effective credit rate in $/kWh. If tariff and an adder are separate "
        "columns, choose the COMBINED rate.\n"
        "- amount: the dollar amount the offtaker is billed or credited each period.\n\n"
        "Also give header_row: the 0-based index of the row holding the column headers.\n\n"
        'Respond with ONLY a JSON object, no prose:\n'
        '{"header_row": <int>, "columns": {"period": <int|null>, "generation": <int|null>, '
        '"consumption": <int|null>, "rate": <int|null>, "amount": <int|null>}, '
        '"confidence": <number 0..1>, "reasoning": "<one short sentence>"}\n'
        "If you cannot confidently find a generation column, set it null and confidence below 0.4."
    )


def _call_anthropic(prompt: str, timeout: int = 22, max_tokens: int = 600) -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": _model(),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(_API_URL, data=body, headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = data.get("content") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


def _parse(text: str, width: int, n_rows: int) -> Optional[dict]:
    """Extract + validate the model's JSON into a mapping partial, or None."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    try:
        hr = int(obj.get("header_row"))
    except (TypeError, ValueError):
        return None
    if hr < 0 or hr >= max(n_rows, 1):
        return None
    raw_cols = obj.get("columns") or {}
    cols: dict[str, int] = {}
    for f in _FIELDS:
        v = raw_cols.get(f)
        if v is None:
            continue
        try:
            ci = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= ci < max(width, 1):
            cols[f] = ci
    if "generation" not in cols:
        return None
    # reject a column claimed by two fields (keep the first by field order)
    seen: set[int] = set()
    deduped: dict[str, int] = {}
    for f in _FIELDS:
        if f in cols and cols[f] not in seen:
            deduped[f] = cols[f]
            seen.add(cols[f])
    if "generation" not in deduped:
        return None
    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_CONFIDENCE:
        return None
    return {"header_row": hr, "columns": deduped, "confidence": conf,
            "reasoning": str(obj.get("reasoning") or "")[:240]}


def ai_map_columns(grid: list[list], sheet: Optional[str],
                   offtaker_name: Optional[str]) -> Optional[dict]:
    """Map the sheet's columns with the model. Returns {header_row, columns,
    confidence, reasoning} or None on any failure (caller falls back to heuristic)."""
    if not ai_available() or not grid:
        return None
    try:
        grid_text, width = _render_grid(grid)
        text = _call_anthropic(_prompt(grid_text, sheet, offtaker_name))
        result = _parse(text or "", width, min(len(grid), _MAX_ROWS))
        if result:
            logger.info("sheet_tracker_ai: mapped via %s conf=%.2f cols=%s",
                        _model(), result["confidence"], result["columns"])
        return result
    except Exception as e:  # noqa: BLE001 — NEVER break upload over the AI path
        logger.warning("sheet_tracker_ai: mapping failed (%s) — falling back to heuristic",
                       type(e).__name__)
        return None


# ─── AI row planner: understand the sheet → append the missing months → validate ─────

def _render_rows(headers: list, rows: list) -> str:
    lines = ["HEADERS: " + " | ".join(f"[{i}] {_trunc(h, 30)}" for i, h in enumerate(headers))]
    for ri, r in enumerate(rows):
        cells = " | ".join(f"[{i}] {_trunc(c, 22)}" for i, c in enumerate(r))
        lines.append(f"sheet row (recent-{len(rows) - ri}): {cells}")
    return "\n".join(lines)


def _render_facts(facts: list) -> str:
    out = []
    for f in facts:
        out.append("- {period}: month={month} start={start} end={end} whole_kwh={whole_kwh} "
                   "share_kwh={share_kwh} rate={rate} amount={amount}".format(**f))
    return "\n".join(out)


def _parse_plan(text: str, ncol: int) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    raw = obj.get("rows")
    if not isinstance(raw, list):
        return None
    rows = []
    for rr in raw:
        if not isinstance(rr, dict):
            continue
        clean = {}
        for k, v in rr.items():
            try:
                ci = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= ci < max(ncol, 1):
                clean[ci] = v
        if clean:
            rows.append(clean)
    if not rows:
        return None
    return {"rows": rows, "explanation": str(obj.get("explanation") or "")[:400],
            "sane": bool(obj.get("sane", True))}


def ai_plan_rows(headers: list, recent_rows: list, facts: list,
                 sheet: Optional[str] = None, offtaker: Optional[str] = None,
                 present_labels: Optional[list] = None, timeout: int = 45) -> Optional[dict]:
    """The intelligence Ford asked for: hand the model the sheet's headers + last rows and the
    customer's available billing periods (each with REAL figures we computed), and let it (1)
    decide which periods are MISSING from the sheet, (2) produce a COMPLETE row for each, matching
    the sheet's columns by continuing its patterns, and (3) say whether the result is consistent.
    Returns {rows:[{col_index:value}], explanation, sane} or None (caller falls back to heuristic).
    The model arranges/validates; the dollar figures it places come from facts we computed — it is
    not asked to invent billing math."""
    if not ai_available() or not facts:
        return None
    try:
        prompt = (
            "You maintain a solar-generation tracking spreadsheet a utility operator hand-built. It "
            "records ONE row per billing month for a single customer"
            + (f' ("{offtaker}")' if offtaker else "") + (f' on sheet "{sheet}"' if sheet else "") + ".\n\n"
            + _render_rows(headers, recent_rows) + "\n"
            + (("ALL MONTHS ALREADY IN THE SHEET (the complete set already present — do NOT re-add "
                "any of these): " + ", ".join(str(p) for p in present_labels) + "\n") if present_labels else "")
            + "\n"
            "Here are the customer's billing periods available from the utility, each with the REAL "
            "figures already computed (whole-array generation, the customer's share kWh, the credit "
            "rate $/kWh, the billed amount, and the period start/end dates):\n"
            + _render_facts(facts) + "\n\n"
            "TASK:\n"
            "1. Decide which of these billing periods are MISSING from the sheet (in the bills above "
            "but not among the recent rows). The sheet may have had recent rows deleted.\n"
            "2. For each missing period, oldest first, produce a COMPLETE new row matching this sheet's "
            "columns exactly, continuing its own patterns:\n"
            "   - the month/period column in the SAME format the existing rows use,\n"
            "   - date columns from the period start/end,\n"
            "   - the whole-array kWh column AND the customer-share kWh column,\n"
            "   - the billed-amount column from the figures above (do not invent it),\n"
            "   - MIMIC THE SHEET EXACTLY: any column holding the SAME value in every existing row (a "
            "flat tariff, a zero adder) MUST carry that EXACT constant forward — do NOT substitute a "
            "different value even if the utility figures suggest one. Only use the utility figures for "
            "columns that genuinely VARY between rows (generation, the customer share, the dates),\n"
            "   - match the existing rows' DATE FORMAT exactly (if they read 7/18/2025, write 8/18/2025 — "
            "not 2025-08-18),\n"
            "   - an invoice-number column incremented by 1,\n"
            "   - a formula column copied as an Excel formula whose row references point at the NEW row,\n"
            "   - leave a column null when you genuinely cannot determine it (e.g. a 'date paid').\n"
            "3. Say whether the rows are internally consistent.\n\n"
            "Respond with ONLY JSON, no prose:\n"
            '{"rows": [{"<col_index>": <value or null>, ...}, ...], '
            '"explanation": "<two sentences: what you added and why it is consistent>", '
            '"sane": <true|false>}\n'
            'Dates as "YYYY-MM-DD". Numbers as numbers. Match the existing month-format exactly. '
            "If nothing is missing, return an empty rows list."
        )
        text = _call_anthropic(prompt, timeout=timeout, max_tokens=2200)
        result = _parse_plan(text or "", len(headers))
        if result is not None:
            logger.info("sheet_tracker_ai: planned %d row(s) via %s sane=%s",
                        len(result["rows"]), _model(), result["sane"])
        return result
    except Exception as e:  # noqa: BLE001 — NEVER break the reconcile over the AI path
        logger.warning("sheet_tracker_ai: plan failed (%s)", type(e).__name__)
        return None
