"""
Reproduce an offtaker's invoice IN THE OPERATOR'S OWN TEMPLATE — the research-
validated hybrid (deep-research wf_f378d415, Jun 2026): never convert the template
to HTML (the lossy anti-pattern); CODE writes the offtaker's values into the real
.xlsx, the real engine (Gotenberg) renders it pixel-perfect, a fail-closed guard
confirms the amount before trust.

WHY DIRECT-CELL-WRITE (not a ledger-row fill): an operator template is reused
across offtakers, but the Template invoice sheet's display cells hold the
template's OWN sample customer's numbers/terms (a flat "Amount Due", a baked-in
"Billing Rate: 88.5%"). Filling a Data-ledger row and re-pointing INDIRECT() does
NOT change those — it recomputes with the TEMPLATE's terms. Empirically that
rendered the sample's $2,150 for a $3,167 offtaker (the guard caught it). So we
LOCATE the dynamic display cells (matcher._build_token_map — the same tokenizer
the HTML path uses) and write THIS offtaker's computed values straight into them,
then swap the sample customer's name for the offtaker's. Values are computed in
Python; the cell's own number format renders them.

reproduce_in_template is FAIL-CLOSED on both amount (verify guard) and identity
(the offtaker name must render, else refuse). Mapping is deterministic for the
HCT family (no LLM); ai_field_map is the novel-template fallback. NOT wired into
the live send path until verified.
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
from typing import Optional

from .pipeline import ReproResult, reproduce_invoice

log = logging.getLogger(__name__)

# Token (from matcher._build_token_map) → key in the offtaker values dict.
_TOKEN_KEYS = ("amount_due", "kwh", "solar_value", "billed_value", "solar_savings",
               "period_start", "period_end", "invoice_number", "invoice_date", "due_date")


def offtaker_values_from_match(match, *, invoice_date: Optional[_dt.date] = None,
                               due_days: int = 28) -> dict:
    """Raw (un-formatted) values to write into the template's mapped cells, taken
    from the offtaker's own computed invoice. The cell's number format renders them."""
    ci = match.computed_invoice or {}
    lp = match.latest_period
    inv_d = invoice_date or _dt.date.today()
    vals = {
        "amount_due": ci.get("amount_owed"),
        "kwh": ci.get("kwh", getattr(lp, "customer_kwh", None)),
        "solar_value": ci.get("solar_value"),
        "billed_value": ci.get("billed_value"),
        "solar_savings": ci.get("solar_savings"),
        "period_start": getattr(lp, "start", None),
        "period_end": getattr(lp, "end", None),
        "invoice_number": ci.get("invoice_number"),
        "invoice_date": inv_d,
        "due_date": inv_d + _dt.timedelta(days=due_days),
    }
    return {k: v for k, v in vals.items() if v is not None}


def build_template_cell_map(template_bytes: bytes) -> Optional[dict]:
    """Cacheable mapping of the operator template's invoice sheet: which dynamic
    display cells hold which field, plus the template's sample customer name (so we
    can swap it). Deterministic via the matcher for the HCT family. Returns
    {sheet, cells:{(r,c):token}, sample_name} or None if the template can't be read."""
    from openpyxl import load_workbook
    from ..matcher import (find_invoice_sheet, _content_bounds, _build_token_map,
                           match_billing_workbook)
    try:
        wb = load_workbook(io.BytesIO(template_bytes), data_only=True)
    except Exception as e:  # noqa: BLE001
        log.warning("build_template_cell_map: cannot open template: %s", e)
        return None
    try:
        ws = find_invoice_sheet(wb) or (wb.worksheets[0] if wb.worksheets else None)
        if ws is None:
            return None
        r0, r1, c0, c1 = _content_bounds(ws, 70, 16)
        covered: set = set()
        for m in ws.merged_cells.ranges:
            for r in range(m.min_row, m.max_row + 1):
                for c in range(m.min_col, m.max_col + 1):
                    if (r, c) != (m.min_row, m.min_col):
                        covered.add((r, c))
        tok = _build_token_map(ws, r0, r1, c0, c1, covered)
        cells = {rc: t.strip("{} ").split()[0] for rc, t in tok.items()}  # "{{ x }}" → "x"
    finally:
        try:
            wb.close()
        except Exception:
            pass
    sample_name = None
    try:
        sample_name = (match_billing_workbook(template_bytes, allow_llm=False).customer or {}).get("name")
    except Exception:  # noqa: BLE001
        pass
    return {"sheet": ws.title, "cells": cells, "sample_name": sample_name}


def _fill_template_cells(template_bytes: bytes, cell_map: dict, values: dict,
                         customer_name: str) -> Optional[bytes]:
    """Write `values` into the mapped display cells of the template's invoice sheet
    and swap the sample customer's name for `customer_name`. Returns new xlsx bytes
    (formulas/styles elsewhere untouched), or None on failure."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(template_bytes))          # keep formulas
    except Exception as e:  # noqa: BLE001
        log.warning("_fill_template_cells: cannot open template: %s", e)
        return None
    sheet = cell_map.get("sheet")
    ws = wb[sheet] if sheet in wb.sheetnames else (wb.worksheets[0] if wb.worksheets else None)
    if ws is None:
        return None
    # 1) Write this offtaker's value into each mapped display cell (overwriting the
    #    template's sample value / formula). Number formats render the raw values.
    for (r, c), token in (cell_map.get("cells") or {}).items():
        if token in values:
            ws.cell(row=r, column=c).value = values[token]
    # 2) Swap the sample customer's name for the offtaker's, wherever it appears —
    #    exact-match cells become the offtaker name; cells that merely CONTAIN it
    #    (e.g. "Valley Village (Valley Cares, Inc)") get the name substring replaced,
    #    so no template-sample identity leaks onto the offtaker's invoice.
    sample = (cell_map.get("sample_name") or "").strip()
    wrote_name = False
    if sample:
        for sh in wb.worksheets:
            for row in sh.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and sample in cell.value:
                        cell.value = (customer_name if cell.value.strip() == sample
                                      else cell.value.replace(sample, customer_name))
                        wrote_name = True
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue() if (wrote_name or not sample) else out.getvalue()


def reproduce_in_template(template_bytes: bytes, *, offtaker_match,
                          customer_name: str,
                          cell_map: Optional[dict] = None,
                          invoice_date: Optional[_dt.date] = None,
                          verify: bool = False) -> Optional[ReproResult]:
    """Reproduce ONE offtaker's invoice in the operator's template, pixel-perfect.

    Writes the offtaker's computed values into the template's mapped display cells
    + swaps in their name, renders, and gates fail-closed on BOTH the amount (verify
    guard) and identity (the name must render). Returns None (caller falls back to
    the standard invoice) when the template can't be mapped, has no Amount-Due cell,
    or the result can't be verified — never ships a wrong amount or wrong name."""
    from .verify import _pdf_lines

    cm = cell_map or build_template_cell_map(template_bytes)
    if not cm or "amount_due" not in set((cm.get("cells") or {}).values()):
        log.info("reproduce_in_template: no mapped Amount Due cell — refusing")
        return None
    values = offtaker_values_from_match(offtaker_match, invoice_date=invoice_date)
    expected = values.get("amount_due")

    def fill(_field_map_override):
        filled = _fill_template_cells(template_bytes, cm, values, customer_name)
        if filled is None:
            raise RuntimeError("template cell fill failed")
        return filled

    res = reproduce_invoice(fill, expected_amount=expected, verify=verify)

    # Identity guard: the offtaker's name MUST appear on the render.
    if res and res.pdf:
        lines = _pdf_lines(res.pdf)
        nm = (customer_name or "").strip().lower()
        if nm and (lines is None or not any(nm in ln.lower() for ln in lines)):
            log.warning("reproduce_in_template: name %r not on render — refusing", customer_name)
            return None
    return res


def _template_self_values(template_bytes: bytes, cell_map: dict) -> dict:
    """The template's OWN (internally-coherent) values at each mapped cell, read
    data_only so formula cells yield their cached result. Used to echo a faithful,
    self-consistent sample through the reproduction pipeline for the settings preview."""
    from openpyxl import load_workbook
    out: dict = {}
    try:
        wb = load_workbook(io.BytesIO(template_bytes), data_only=True)
    except Exception:  # noqa: BLE001
        return out
    sheet = cell_map.get("sheet")
    ws = wb[sheet] if sheet in wb.sheetnames else (wb.worksheets[0] if wb.worksheets else None)
    if ws is not None:
        for (r, c), token in (cell_map.get("cells") or {}).items():
            v = ws.cell(row=r, column=c).value
            if v is not None:
                out[token] = v
    try:
        wb.close()
    except Exception:
        pass
    return out


def reproduce_template_preview(template_bytes: bytes, *,
                               sample_name: str = "Sample Offtaker") -> Optional[bytes]:
    """OUR reproduction of the operator's template for the settings preview — the
    SAME direct-cell-write pipeline a real send uses, so the operator sees exactly
    what our engine produces (and can spot any infidelity), not their raw upload.

    Echoes the template's own coherent numbers (consistent with its rate labels) and
    swaps in a clearly-sample bill-to so the pane is visibly our reconstruction.
    Returns rendered PDF bytes, or None to fall back to a plain render."""
    from .render import render_office_to_pdf, renderer_available
    if not renderer_available():
        return None
    cm = build_template_cell_map(template_bytes)
    if not cm:
        return None
    values = _template_self_values(template_bytes, cm)
    filled = _fill_template_cells(template_bytes, cm, values, sample_name)
    if filled is None:
        return None
    try:
        return render_office_to_pdf(filled, "reproduction_preview.xlsx")
    except Exception as e:  # noqa: BLE001
        log.warning("reproduce_template_preview render failed: %s", e)
        return None
