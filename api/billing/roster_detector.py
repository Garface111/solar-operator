"""Format-agnostic column detector for the Array Operator offtaker-roster upload.

Ford's ask (2026-07-01): an operator uploads ANY spreadsheet — unknown headers,
any column order, junk/title rows above the header — and we detect which column is
which of our roster fields, then let them confirm/correct the mapping before we
create offtakers. This is BILLING, so it is deterministic + reviewable and NEVER a
silent wrong guess: every mapped field carries a confidence, weak ones surface for
review, and the operator can override the whole mapping.

Reuses the proven generation-report detection engine rather than reinventing it:
  * grid readers + sheet selection  ── sheet_tracker._open_grid / _read_xlsx_grid /
      _read_csv_grid / _looks_like_csv
  * header-candidate scan            ── the same weighted-keyword approach as
      sheet_tracker._score_cell, adapted to a ROSTER keyword set here.
  * content coercion helpers         ── matcher._s (normalize) / matcher._num (float
      tolerating $/%/commas) and sheet_tracker._looks_numeric.
  * the STRONGEST content signal     ── offtaker_match.match_array(): the column whose
      values fuzzy-match the operator's real arrays IS the array column, regardless of
      what its header says.
  * optional LLM header fallback     ── sheet_tracker_ai.ai_available()/_call_anthropic
      (ANTHROPIC_API_KEY gated), used ONLY on the header row, never on data cells.

Philosophy: HEADER-FIRST, CONTENT-SECOND. Header keywords propose a mapping; content
sniffing is the tiebreaker AND the safety net (it wins when headers are junk). A greedy
bipartite assignment gives each field its best available column with no column reused.

Pure + deterministic given (file_bytes, arrays, utility_accounts) except the optional
LLM fallback. Never raises — a detection failure returns ok=False with clear warnings.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .matcher import _num, _s
from .offtaker_match import match_array
from .sheet_tracker import (
    _looks_numeric,
    _open_grid,
    _read_csv_grid,
    _read_xlsx_grid,
)

# ─── target fields ────────────────────────────────────────────────────────────

# The seven roster fields we detect. offtaker_name / array_name / allocation_pct are
# REQUIRED for a usable import; the rest are optional enrichments.
TARGET_FIELDS: tuple[str, ...] = (
    "offtaker_name",
    "array_name",
    "allocation_pct",
    "email",
    "discount_pct",
    "net_rate",
    "account_number",
)
REQUIRED_FIELDS: tuple[str, ...] = ("offtaker_name", "array_name", "allocation_pct")

# ─── header keyword sets (ranked weights per field) ───────────────────────────
#
# Same weighted-keyword model as sheet_tracker._score_cell: a header cell scores for
# a field by the best (weight × len(keyword)) match it contains, with an exact-cell
# bonus. Weights are ranked so a specific, unit/role-bearing token out-ranks a vague
# one that happens to share the cell (e.g. "allocation %" 4.0 beats a bare "share" 1.5).

_ROSTER_FIELD_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "offtaker_name": [
        ("offtaker name", 3.5), ("customer name", 3.5), ("tenant", 2.0),
        ("offtaker", 2.0), ("customer", 1.5), ("subscriber", 2.0),
        ("member", 1.5), ("name", 2.0),
    ],
    "array_name": [
        ("array name", 3.5), ("site", 2.5), ("project", 2.0), ("system", 1.8),
        ("array", 2.0), ("generator", 1.5), ("facility", 1.8), ("installation", 1.5),
    ],
    "email": [
        ("email", 3.0), ("e-mail", 3.0), ("contact", 2.0),
    ],
    "allocation_pct": [
        ("allocation %", 4.0), ("share %", 3.5), ("allocation pct", 4.0),
        ("percent", 2.5), ("allocation", 2.0), ("share", 1.5),
        ("%", 2.0), ("pct", 2.0), ("portion", 1.8),
    ],
    "discount_pct": [
        ("discount %", 3.5), ("discount", 2.5), ("savings", 1.5), ("markdown", 1.5),
    ],
    "net_rate": [
        ("$/kwh", 4.0), ("credit rate", 3.0), ("net rate", 3.0), ("rate", 2.0),
        ("price", 1.5), ("$ / kwh", 4.0), ("per kwh", 3.0),
    ],
    "account_number": [
        ("gmp account", 3.0), ("account", 2.5), ("acct", 2.0), ("meter", 2.0),
        ("account #", 3.0), ("account number", 3.0),
    ],
}

# A discount column, when unlabeled, is usually the SECOND percentage column (allocation
# is the first). We use this only as a tiebreaker in the content pass.

_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")

# How many data rows we sniff per column for the content pass (cap for big files).
_MAX_SNIFF_ROWS = 60
# How many rows of the top of the sheet we scan for the header.
_MAX_HEADER_SCAN = 15


# ─── header scoring (adapted from sheet_tracker._score_cell) ──────────────────

def _score_roster_cell(cell: str) -> dict[str, tuple[float, str]]:
    """For one normalized header cell, return {field: (score, matched_keyword)} for
    every roster field whose keyword appears in the cell.

    Same model as sheet_tracker._score_cell: score = weight × (len(kw)+1) − position
    penalty, with a big exact-cell bonus. The per-keyword weight lets a specific token
    (e.g. 'allocation %' → allocation_pct) out-rank a vague one sharing the cell."""
    out: dict[str, tuple[float, str]] = {}
    if not cell:
        return out
    for field, kws in _ROSTER_FIELD_KEYWORDS.items():
        best: Optional[tuple[float, str]] = None
        for kw, weight in kws:
            idx = cell.find(kw)
            if idx < 0:
                continue
            score = weight * (len(kw) + 1.0) - idx * 0.5
            if cell == kw:
                score += 50.0
            if best is None or score > best[0]:
                best = (score, kw)
        if best is not None:
            out[field] = best
    return out


def _find_roster_header(grid: list[list]) -> tuple[Optional[int], list[str]]:
    """Scan the first ~15 rows for the row that best looks like a roster header (the
    most fields matched, weighted by confidence). Returns (header_row_idx, raw_headers).

    Independent of sheet_tracker._find_header because that one REQUIRES a 'generation'
    column; a roster has no generation column. We only need the header ROW here — the
    per-column field assignment is done later by combining header + content scores."""
    best_row: Optional[int] = None
    best_headers: list[str] = []
    best_score = -1.0
    for ridx in range(min(len(grid), _MAX_HEADER_SCAN)):
        row = grid[ridx]
        # field -> best score seen on this row
        claims: dict[str, float] = {}
        nonblank = 0
        for cell in row:
            cell_n = _s(cell)
            if cell_n:
                nonblank += 1
            for field, (sc, _kw) in _score_roster_cell(cell_n).items():
                if field not in claims or sc > claims[field]:
                    claims[field] = sc
        # A header row should look like labels, not data: reward fields matched + a
        # small bonus for having several non-blank text cells, penalize rows that are
        # mostly numeric (those are data rows).
        total = sum(claims.values())
        numeric_cells = sum(1 for c in row if _looks_numeric(c))
        if nonblank:
            total -= numeric_cells * 1.5  # data-looking row → not a header
        if total > best_score and claims:
            best_score = total
            best_row = ridx
            best_headers = [str(c).strip() if c is not None else "" for c in row]
    return best_row, best_headers


# ─── content sniffing (the crucial add: judge a column by its DATA) ───────────

def _column_values(grid: list[list], header_row: int, col: int) -> list[Any]:
    """The non-empty data cells in one column below the header (capped)."""
    out: list[Any] = []
    for row in grid[header_row + 1:]:
        if col < len(row):
            v = row[col]
            if v is not None and str(v).strip():
                out.append(v)
        if len(out) >= _MAX_SNIFF_ROWS:
            break
    return out


def _frac_email(values: list[Any]) -> float:
    if not values:
        return 0.0
    hits = sum(1 for v in values if _EMAIL_RE.search(str(v)))
    return hits / len(values)


def _frac_array_match(values: list[Any], arrays: list[dict],
                      utility_accounts: list[dict]) -> float:
    """Fraction of cells whose value fuzzy-matches one of the operator's real arrays
    with confidence exact/high/medium (via offtaker_match.match_array). This is the
    STRONGEST signal: the column whose values ARE the operator's arrays is the array
    column, whatever its header says."""
    if not values or not (arrays or utility_accounts):
        return 0.0
    good = 0
    for v in values:
        try:
            r = match_array(str(v), arrays, utility_accounts)
        except Exception:  # noqa: BLE001 — a matcher hiccup must not break detection
            continue
        if r.get("confidence") in ("exact", "high", "medium"):
            good += 1
    return good / len(values)


def _frac_numeric(values: list[Any]) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if _looks_numeric(v)) / len(values)


def _looks_percentage(values: list[Any]) -> float:
    """Fraction of numeric cells whose magnitude looks like a percentage/share.
    Note matcher._num('50%') → 0.50 (it divides a trailing-% value by 100), so a share
    lands in (0,1]; a bare '50' lands at 50. Both are percentage-shaped, so accept
    values in (0, 1] OR (1, 100]."""
    nums = [_num(v) for v in values]
    nums = [n for n in nums if n is not None]
    if not nums:
        return 0.0
    ok = sum(1 for n in nums if 0.0 < n <= 100.0)
    return ok / len(nums)


def _looks_rate(values: list[Any]) -> float:
    """Fraction of numeric cells that look like a $/kWh rate: small decimals (< ~2)."""
    nums = [_num(v) for v in values]
    nums = [n for n in nums if n is not None]
    if not nums:
        return 0.0
    ok = sum(1 for n in nums if 0.0 < n < 2.0)
    return ok / len(nums)


def _frac_free_text(values: list[Any]) -> float:
    """Fraction of cells that are free text (not numeric, not email). The offtaker-name
    column is mostly free text that ISN'T an array/email/number."""
    if not values:
        return 0.0
    txt = 0
    for v in values:
        s = str(v).strip()
        if not s:
            continue
        if _looks_numeric(v):
            continue
        if _EMAIL_RE.search(s):
            continue
        txt += 1
    return txt / len(values)


def _frac_account_id(values: list[Any], account_numbers: set[str]) -> float:
    """Fraction of cells that look like an account/meter id: alphanumeric ID-ish, OR an
    exact match to one of the tenant's known utility-account numbers."""
    if not values:
        return 0.0
    hits = 0
    for v in values:
        s = str(v).strip()
        if not s:
            continue
        if s in account_numbers:
            hits += 1
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]{3,}", s) and any(ch.isdigit() for ch in s):
            hits += 1
    return hits / len(values)


def _content_scores(grid: list[list], header_row: int, ncols: int,
                    arrays: list[dict], utility_accounts: list[dict],
                    account_numbers: set[str]) -> list[dict[str, float]]:
    """Per column, a {field: content_score in ~0..1+} dict from sniffing the DATA cells.
    These are the fractions/heuristics that make detection format-agnostic — headers can
    lie, the values below rarely do."""
    per_col: list[dict[str, float]] = []
    # First pass: gather per-column raw signals.
    raw: list[dict[str, float]] = []
    for col in range(ncols):
        values = _column_values(grid, header_row, col)
        raw.append({
            "n": float(len(values)),
            "email": _frac_email(values),
            "array": _frac_array_match(values, arrays, utility_accounts),
            "numeric": _frac_numeric(values),
            "pct": _looks_percentage(values),
            "rate": _looks_rate(values),
            "text": _frac_free_text(values),
            "acct": _frac_account_id(values, account_numbers),
        })
    # Identify which numeric columns are percentage-shaped, left→right, so the FIRST is
    # allocation and a SECOND is discount (the common unlabeled convention).
    pct_cols = [c for c in range(ncols)
                if raw[c]["n"] > 0 and raw[c]["numeric"] >= 0.6 and raw[c]["pct"] >= 0.6]
    first_pct = pct_cols[0] if pct_cols else None
    second_pct = pct_cols[1] if len(pct_cols) > 1 else None

    for col in range(ncols):
        r = raw[col]
        s: dict[str, float] = {}
        if r["n"] <= 0:
            per_col.append(s)
            continue
        # email — near-unambiguous
        if r["email"] >= 0.5:
            s["email"] = r["email"]
        # array_name — the strongest content signal, weighted heavily
        if r["array"] >= 0.3:
            s["array_name"] = r["array"] * 1.2
        # allocation_pct / discount_pct — percentage-shaped numeric columns
        if r["numeric"] >= 0.6 and r["pct"] >= 0.6:
            if col == first_pct:
                s["allocation_pct"] = 0.9 + r["pct"] * 0.1
            if col == second_pct:
                s["discount_pct"] = 0.6 + r["pct"] * 0.1
            # a lone percentage column is far more likely allocation than discount
            if second_pct is None and col == first_pct:
                s.setdefault("allocation_pct", 0.9)
        # net_rate — small-decimal numeric that isn't a percentage share
        if r["numeric"] >= 0.6 and r["rate"] >= 0.6 and r["pct"] < 0.6:
            s["net_rate"] = 0.6 + r["rate"] * 0.2
        # account_number — id-ish / known account number
        if r["acct"] >= 0.5:
            s["account_number"] = r["acct"]
        # offtaker_name — mostly free text, NOT an array/email/number column
        if r["text"] >= 0.6 and r["array"] < 0.3 and r["email"] < 0.3 and r["numeric"] < 0.3:
            s["offtaker_name"] = r["text"]
        per_col.append(s)
    return per_col


# ─── combine + assign ─────────────────────────────────────────────────────────

def _confidence(header_sc: float, content_sc: float, field: str,
                content_raw: float) -> str:
    """Confidence for a (field←column) pick from its header + content agreement.
      high   — header AND content agree, OR a very strong single content signal
               (e.g. array column ≥ .8 match, email ≥ .8).
      medium — one strong signal present.
      low    — weak/ambiguous but the best available.
    'none' is handled by the caller (unmapped fields)."""
    strong_content = content_raw >= 0.8 and field in ("array_name", "email", "offtaker_name")
    if header_sc > 0 and content_sc > 0:
        return "high"
    if strong_content:
        return "high"
    if header_sc >= 15.0 or content_sc >= 0.6:
        return "medium"
    return "low"


def _assign(header_scores: list[dict[str, tuple[float, str]]],
            content_scores: list[dict[str, float]],
            ncols: int) -> dict[str, dict[str, Any]]:
    """Greedy bipartite assignment: build every (field, column, combined_score) triple,
    sort by score desc, and take each in turn while neither the field nor the column is
    already used. Higher total score wins a contested column; weak leftovers stay
    unmapped. Deterministic (ties broken by field order then column index)."""
    # Normalize header scores to a comparable ~0..1+ range so they combine with content
    # fractions. Header scores run ~5..60; divide by 40.
    triples: list[tuple[float, str, int, float, float]] = []  # (combined, field, col, hs_norm, cs)
    for col in range(ncols):
        hsc = header_scores[col] if col < len(header_scores) else {}
        csc = content_scores[col] if col < len(content_scores) else {}
        fields = set(hsc) | set(csc)
        for field in fields:
            hs = hsc.get(field, (0.0, ""))[0]
            cs = csc.get(field, 0.0)
            hs_norm = hs / 40.0
            combined = hs_norm + cs
            triples.append((combined, field, col, hs, cs))
    # Sort: highest combined first; deterministic tiebreak by field name then col.
    triples.sort(key=lambda t: (-t[0], t[1], t[2]))

    used_fields: set[str] = set()
    used_cols: set[int] = set()
    result: dict[str, dict[str, Any]] = {}
    for combined, field, col, hs, cs in triples:
        if field in used_fields or col in used_cols:
            continue
        if combined <= 0:
            continue
        used_fields.add(field)
        used_cols.add(col)
        content_raw = content_scores[col].get(field, 0.0) if col < len(content_scores) else 0.0
        result[field] = {
            "index": col,
            "confidence": _confidence(hs, cs, field, content_raw),
            "_header_score": hs,
            "_content_score": cs,
        }
    return result


# ─── optional LLM header fallback (header row only, never data) ────────────────

def _llm_header_fallback(headers: list[str],
                         needed: list[str]) -> Optional[dict[str, int]]:
    """When a REQUIRED field is unmapped/low and an LLM is available (ANTHROPIC_API_KEY),
    ask it to map JUST the header strings → our fields. Never sends data cells. Reuses
    sheet_tracker_ai's exact call path. Fail-safe: no key / any error → None."""
    try:
        from .sheet_tracker_ai import _call_anthropic, ai_available
    except Exception:  # noqa: BLE001
        return None
    if not ai_available():
        return None
    try:
        import json

        indexed = " | ".join(f"[{i}] {h}" for i, h in enumerate(headers))
        prompt = (
            "You map the column headers of a solar community-subscriber roster so an "
            "automated system can import offtakers. Below are the 0-indexed column headers "
            "(some may be blank or junk):\n\n"
            f"{indexed}\n\n"
            "Identify the single column index for each field (or null if absent):\n"
            "- offtaker_name: the subscriber/customer/tenant name.\n"
            "- array_name: the solar array/site/project the subscriber is allocated to.\n"
            "- allocation_pct: the subscriber's percentage share/allocation.\n"
            "- email: the subscriber's email.\n"
            "- discount_pct: a discount percentage, if present.\n"
            "- net_rate: a credit rate in $/kWh, if present.\n"
            "- account_number: a utility account/meter number, if present.\n\n"
            "Respond with ONLY a JSON object, no prose:\n"
            '{"offtaker_name": <int|null>, "array_name": <int|null>, '
            '"allocation_pct": <int|null>, "email": <int|null>, '
            '"discount_pct": <int|null>, "net_rate": <int|null>, '
            '"account_number": <int|null>}'
        )
        text = _call_anthropic(prompt, timeout=20, max_tokens=400)
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))
        out: dict[str, int] = {}
        for field in TARGET_FIELDS:
            v = obj.get(field)
            if v is None:
                continue
            try:
                ci = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= ci < len(headers):
                out[field] = ci
        return out or None
    except Exception:  # noqa: BLE001 — the LLM path can NEVER break detection
        return None


# ─── public entrypoint ────────────────────────────────────────────────────────

def _account_numbers(utility_accounts: list[dict]) -> set[str]:
    out: set[str] = set()
    for ua in utility_accounts or []:
        acct = ua.get("account_number")
        if acct is not None and str(acct).strip():
            out.add(str(acct).strip())
    return out


def _preview_rows(grid: list[list], header_row: int, ncols: int,
                  n: int = 5) -> list[list[Any]]:
    """Up to `n` data rows below the header, padded to ncols, JSON-safe (str-coerced)."""
    rows: list[list[Any]] = []
    for row in grid[header_row + 1:]:
        if not any(str(c).strip() for c in row if c is not None):
            continue
        cells = [row[c] if c < len(row) else None for c in range(ncols)]
        rows.append([("" if c is None else c) for c in cells])
        if len(rows) >= n:
            break
    return rows


def detect_roster_columns(file_bytes: bytes, filename: str,
                          arrays: list[dict],
                          utility_accounts: list[dict]) -> dict:
    """Detect which column is which roster field in an arbitrary uploaded spreadsheet.

    Args:
      file_bytes: the raw uploaded .xlsx / .csv bytes.
      filename:   used only to sniff csv-vs-xlsx.
      arrays:      [{id, name}, ...] — the tenant's arrays (for content array-matching).
      utility_accounts: [{utility_account_id, array_id, array_name, nickname, provider,
                          has_bill, account_number?}, ...] — same shape match_array reads.

    Returns (JSON-serializable):
      { ok, sheet, header_row (0-based), headers: [str...],
        column_map: { <field>: {index, header, confidence:"high|medium|low"} | None
                      for each target field },
        unmapped_columns: [{index, header, sample:[first ~3 non-empty values]}],
        preview: [ [cell,...] x up to 5 data rows ],
        data_rows: int, via: "heuristic|content|mixed|llm", warnings: [str] }

    Deterministic given the inputs, except the optional LLM header fallback. Never
    raises — a failure returns ok=False with warnings.
    """
    warnings: list[str] = []
    arrays = arrays or []
    utility_accounts = utility_accounts or []

    # 1) Load the grid (reuse the proven readers; they pick the best sheet).
    try:
        grid, sheet, _kind = _open_grid(file_bytes, filename)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sheet": None, "header_row": None, "headers": [],
                "column_map": {f: None for f in TARGET_FIELDS},
                "unmapped_columns": [], "preview": [], "data_rows": 0,
                "via": "heuristic", "warnings": [f"Could not open the file: {e}"]}
    if not grid:
        return {"ok": False, "sheet": sheet, "header_row": None, "headers": [],
                "column_map": {f: None for f in TARGET_FIELDS},
                "unmapped_columns": [], "preview": [], "data_rows": 0,
                "via": "heuristic", "warnings": ["The file appears to be empty."]}

    # 2) Find the header row.
    header_row, headers = _find_roster_header(grid)
    if header_row is None:
        header_row = 0
        headers = [str(c).strip() if c is not None else "" for c in (grid[0] if grid else [])]
        warnings.append("Couldn't confidently find a header row; assuming the first row.")

    ncols = max((len(r) for r in grid), default=0)
    # Pad headers to ncols.
    headers = [(headers[i] if i < len(headers) else "") for i in range(ncols)]

    # 3) Score each column by header keywords AND by sniffing its data cells.
    header_scores = [_score_roster_cell(_s(h)) for h in headers]
    acct_nums = _account_numbers(utility_accounts)
    content_scores = _content_scores(grid, header_row, ncols, arrays,
                                     utility_accounts, acct_nums)

    # 4) Greedy bipartite assignment.
    assigned = _assign(header_scores, content_scores, ncols)

    # Determine provenance (heuristic/content/mixed) from what drove each pick.
    any_header = any(v["_header_score"] > 0 for v in assigned.values())
    any_content = any(v["_content_score"] > 0 for v in assigned.values())
    via = "mixed" if (any_header and any_content) else ("content" if any_content else "heuristic")

    # 5) Optional LLM header fallback if a REQUIRED field is unmapped or low-confidence.
    required_weak = [f for f in REQUIRED_FIELDS
                     if f not in assigned or assigned[f]["confidence"] == "low"]
    if required_weak:
        llm = _llm_header_fallback(headers, required_weak)
        if llm:
            used_cols = {v["index"] for v in assigned.values()}
            for field in required_weak:
                col = llm.get(field)
                if col is None:
                    continue
                # Don't clobber a confident existing pick; only fill/upgrade weak ones,
                # and never steal a column already assigned to another field.
                if field in assigned and assigned[field]["confidence"] != "low":
                    continue
                if col in used_cols and (field not in assigned or assigned[field]["index"] != col):
                    continue
                assigned[field] = {"index": col, "confidence": "medium",
                                   "_header_score": 0.0, "_content_score": 0.0,
                                   "_llm": True}
                used_cols.add(col)
                via = "llm"

    # 6) Build the column_map (every target field present, None when unmapped).
    column_map: dict[str, Optional[dict]] = {}
    for field in TARGET_FIELDS:
        pick = assigned.get(field)
        if pick is None:
            column_map[field] = None
            continue
        idx = pick["index"]
        column_map[field] = {
            "index": idx,
            "header": headers[idx] if idx < len(headers) else "",
            "confidence": pick["confidence"],
        }

    # 7) Unmapped columns (for the operator to eyeball / hand-map).
    mapped_cols = {v["index"] for v in assigned.values()}
    unmapped_columns: list[dict] = []
    for col in range(ncols):
        if col in mapped_cols:
            continue
        vals = _column_values(grid, header_row, col)[:3]
        unmapped_columns.append({
            "index": col,
            "header": headers[col] if col < len(headers) else "",
            "sample": [("" if v is None else str(v)) for v in vals],
        })

    preview = _preview_rows(grid, header_row, ncols, n=5)
    data_rows = sum(1 for row in grid[header_row + 1:]
                    if any(c is not None and str(c).strip() for c in row))

    # Warn on required fields that are unmapped or low.
    for f in REQUIRED_FIELDS:
        if column_map.get(f) is None:
            warnings.append(f"Required field '{f}' could not be detected — map it manually.")
        elif column_map[f]["confidence"] == "low":
            warnings.append(f"Required field '{f}' was detected with LOW confidence — please confirm.")

    ok = all(column_map.get(f) is not None for f in REQUIRED_FIELDS)

    return {
        "ok": ok,
        "sheet": sheet,
        "header_row": header_row,
        "headers": headers,
        "column_map": column_map,
        "unmapped_columns": unmapped_columns,
        "preview": preview,
        "data_rows": data_rows,
        "via": via,
        "warnings": warnings,
    }


# ─── self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import io

    def _to_csv_bytes(grid: list[list]) -> bytes:
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        for row in grid:
            w.writerow(["" if c is None else c for c in row])
        return buf.getvalue().encode("utf-8")

    # The operator's real arrays (what content-matching resolves against).
    ARRAYS = [
        {"id": 1, "name": "Maple Street Solar (53984)"},
        {"id": 2, "name": "Route 7 Community Array"},
        {"id": 3, "name": "Hilltop Farm"},
    ]
    UACCTS = [
        {"utility_account_id": 101, "array_id": 1, "array_name": "Maple Street Solar",
         "nickname": "Maple St", "provider": "gmp", "account_number": "12345",
         "has_bill": True},
        {"utility_account_id": 102, "array_id": 2, "array_name": "Route 7 Community",
         "nickname": None, "provider": "gmp", "account_number": "22222", "has_bill": True},
        {"utility_account_id": 103, "array_id": 3, "array_name": "Hilltop Farm",
         "nickname": None, "provider": "vec", "account_number": "33333", "has_bill": True},
    ]

    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        status = "ok " if cond else "FAIL"
        print(f"  [{status}] {label}" + (f"  — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    def mapped(res: dict, field: str) -> Optional[int]:
        cm = res["column_map"].get(field)
        return cm["index"] if cm else None

    print("detect_roster_columns self-test:\n")

    # ── Case A: clean, well-labeled sheet, canonical order ──
    gridA = [
        ["Offtaker Name", "Array Name", "Allocation %", "Email"],
        ["Alice Cooper", "Maple Street Solar", "50%", "alice@example.com"],
        ["Bob Dylan", "Route 7 Community Array", "30%", "bob@example.com"],
        ["Carol King", "Hilltop Farm", "20%", "carol@example.com"],
    ]
    resA = detect_roster_columns(_to_csv_bytes(gridA), "roster.csv", ARRAYS, UACCTS)
    print(" Case A — clean labeled sheet")
    check("A ok", resA["ok"], str(resA["warnings"]))
    check("A offtaker_name=0", mapped(resA, "offtaker_name") == 0, str(mapped(resA, "offtaker_name")))
    check("A array_name=1", mapped(resA, "array_name") == 1, str(mapped(resA, "array_name")))
    check("A allocation_pct=2", mapped(resA, "allocation_pct") == 2, str(mapped(resA, "allocation_pct")))
    check("A email=3", mapped(resA, "email") == 3, str(mapped(resA, "email")))

    # ── Case B: JUNK title rows above the header, weird headers, REORDERED columns.
    # Headers are unhelpful ("Solar Site", "% of System", "Customer", "Contact"); the
    # array column must be found by CONTENT (its values are the real arrays). ──
    gridB = [
        ["Community Solar Roster — Q3 2026", None, None, None, None],
        ["Generated 2026-07-01. Confidential.", None, None, None, None],
        [None, None, None, None, None],
        ["Solar Site", "% of System", "Customer", "Contact", "Discount"],
        ["Maple Street Solar", "0.50", "Alice Cooper", "alice@example.com", "0.10"],
        ["Route 7 Community", "0.30", "Bob Dylan", "bob@example.com", "0.05"],
        ["Hilltop Farm", "0.20", "Carol King", "carol@example.com", "0.00"],
    ]
    resB = detect_roster_columns(_to_csv_bytes(gridB), "messy.csv", ARRAYS, UACCTS)
    print(" Case B — junk title rows + weird headers + reordered + content-driven array col")
    check("B header_row=3", resB["header_row"] == 3, str(resB["header_row"]))
    check("B array_name=0 via content", mapped(resB, "array_name") == 0, str(mapped(resB, "array_name")))
    check("B offtaker_name=2", mapped(resB, "offtaker_name") == 2, str(mapped(resB, "offtaker_name")))
    check("B allocation_pct=1", mapped(resB, "allocation_pct") == 1, str(mapped(resB, "allocation_pct")))
    check("B email=3", mapped(resB, "email") == 3, str(mapped(resB, "email")))
    check("B discount_pct=4", mapped(resB, "discount_pct") == 4, str(mapped(resB, "discount_pct")))
    check("B ok (all required found)", resB["ok"], str(resB["warnings"]))

    # ── Case C: totally uninformative headers — array + offtaker found ONLY by content. ──
    gridC = [
        ["A", "B", "C", "D"],
        ["Alice Cooper", "alice@x.com", "Maple Street Solar", "50"],
        ["Bob Dylan", "bob@x.com", "Route 7 Community Array", "30"],
        ["Carol King", "carol@x.com", "Hilltop Farm", "20"],
    ]
    resC = detect_roster_columns(_to_csv_bytes(gridC), "blind.csv", ARRAYS, UACCTS)
    print(" Case C — uninformative headers, everything by content")
    check("C array_name=2 via content", mapped(resC, "array_name") == 2, str(mapped(resC, "array_name")))
    check("C email=1 via content", mapped(resC, "email") == 1, str(mapped(resC, "email")))
    check("C offtaker_name=0 via content", mapped(resC, "offtaker_name") == 0, str(mapped(resC, "offtaker_name")))
    check("C allocation_pct=3 via content", mapped(resC, "allocation_pct") == 3, str(mapped(resC, "allocation_pct")))
    check("C via=content or mixed", resC["via"] in ("content", "mixed"), resC["via"])

    # ── Case D: net_rate + account_number present; array named loosely (typo/subset). ──
    gridD = [
        ["Subscriber", "Site", "Share", "$/kWh", "GMP Account"],
        ["Dave Grohl", "Maple Steet Solar", "40%", "0.1811", "12345"],
        ["Eve Best", "Rte 7 Community", "35%", "0.1811", "22222"],
        ["Finn Wolf", "Hilltop Farm", "25%", "0.1811", "33333"],
    ]
    resD = detect_roster_columns(_to_csv_bytes(gridD), "rates.csv", ARRAYS, UACCTS)
    print(" Case D — net_rate + account_number + loose array names")
    check("D array_name=1", mapped(resD, "array_name") == 1, str(mapped(resD, "array_name")))
    check("D allocation_pct=2", mapped(resD, "allocation_pct") == 2, str(mapped(resD, "allocation_pct")))
    check("D net_rate=3", mapped(resD, "net_rate") == 3, str(mapped(resD, "net_rate")))
    check("D account_number=4", mapped(resD, "account_number") == 4, str(mapped(resD, "account_number")))

    # ── Case E: missing required field (no array column at all) → ok False + warning. ──
    gridE = [
        ["Name", "Allocation %", "Email"],
        ["Alice", "50%", "alice@x.com"],
        ["Bob", "50%", "bob@x.com"],
    ]
    resE = detect_roster_columns(_to_csv_bytes(gridE), "noarray.csv", ARRAYS, UACCTS)
    print(" Case E — required array_name absent")
    check("E ok False", not resE["ok"])
    check("E array_name unmapped", resE["column_map"]["array_name"] is None)
    check("E warns about array_name", any("array_name" in w for w in resE["warnings"]), str(resE["warnings"]))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} case(s): {failures}")
        raise SystemExit(1)
    print("SELF-TEST PASSED — all cases green.")
