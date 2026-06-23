"""
Reproduce an offtaker's invoice IN THE OPERATOR'S OWN TEMPLATE — the research-
validated hybrid (deep-research wf_f378d415, Jun 2026).

The pattern the evidence converged on: never convert the template to HTML (the
lossy end-to-end-generation anti-pattern). Instead the LLM decides only WHERE
data goes (a cell→field map); CODE fills the operator's real .xlsx with openpyxl
(styles/formulas preserved); the real engine (Gotenberg/LibreOffice) renders it
pixel-perfect; a deterministic guard confirms the numbers landed before trust.

Mapping strategy (highest-leverage first):
  1. HCT-family templates (Data ledger + Template sheet + 'New Row #' INDIRECT
     pointer) → the deterministic matcher already yields the field_map. NO LLM.
  2. Novel templates → ai_field_map (strict JSON schema, locate-then-fill) as a
     fallback, refined by the verify loop.
The map is computed ONCE per template and meant to be CACHED on the template
(research's key cost/risk lever — build_template_cell_map returns the cacheable
dict). Values are always computed/formatted in Python, never emitted by the LLM.

Foundation module — NOT yet wired into the live send path. Entry points:
build_template_cell_map(), reproduce_in_template().
"""
from __future__ import annotations

import logging
import types
from typing import Optional

from .analyze import ai_field_map
from .pipeline import ReproResult, reproduce_invoice

log = logging.getLogger(__name__)


def build_template_cell_map(template_bytes: bytes) -> Optional[dict]:
    """Compute the cacheable cell→field map for an operator's Excel template.

    Returns a parsed_map-shaped dict {data_sheet, field_map, billing_model,
    allocation_pct, customer} the deterministic fill understands, or None when the
    template can't be mapped (caller falls back to the standard/HTML path).

    Deterministic HCT match first (free, no LLM); ai_field_map (strict schema) as
    the fallback for novel layouts. Cache the result on the template so the LLM is
    a one-time onboarding step, not a per-invoice call.
    """
    from ..matcher import match_billing_workbook
    m = match_billing_workbook(template_bytes, allow_llm=False)
    if m.matched and m.field_map and "month" in m.field_map:
        return {"data_sheet": m.data_sheet, "field_map": m.field_map,
                "billing_model": m.billing_model, "allocation_pct": m.allocation_pct,
                "customer": m.customer, "source": "matcher"}
    # Novel template — ask the LLM for the map (strict JSON schema, locate-then-fill).
    r = ai_field_map(template_bytes)
    if r and r.get("field_map"):
        return {"data_sheet": r.get("data_sheet"), "field_map": r["field_map"],
                "billing_model": "percent_of_array", "allocation_pct": None,
                "customer": {}, "source": "llm"}
    return None


def _set_template_identity(template_bytes: bytes, data_sheet: Optional[str],
                           name: str) -> Optional[bytes]:
    """Write the offtaker's name into the template's metadata CUSTOMER cell and
    CLEAR the sample's acct/meter, so a template reused for a different offtaker
    doesn't carry the sample customer's identity. Returns new bytes, or None when
    the customer cell can't be located (caller must then refuse — fail closed)."""
    import io as _io
    from openpyxl import load_workbook
    try:
        wb = load_workbook(_io.BytesIO(template_bytes))            # keep formulas
    except Exception:  # noqa: BLE001
        return None
    sheets = [data_sheet] if data_sheet in wb.sheetnames else wb.sheetnames
    for sn in sheets:
        ws = wb[sn]
        for r in range(1, 9):
            cols: dict = {}
            for c in range(1, min(ws.max_column or 0, 18) + 1):
                v = ws.cell(row=r, column=c).value
                if not isinstance(v, str):
                    continue
                lv = v.strip().lower()
                if lv == "customer" or lv.startswith("customer"):
                    cols["name"] = c
                elif "acct" in lv or "account" in lv:
                    cols["acct"] = c
                elif "meter" in lv:
                    cols["meter"] = c
            if "name" in cols and len(cols) >= 2:                  # a real metadata row
                ws.cell(row=r + 1, column=cols["name"]).value = name
                for k in ("acct", "meter"):
                    if k in cols:
                        ws.cell(row=r + 1, column=cols[k]).value = None
                out = _io.BytesIO()
                wb.save(out)
                return out.getvalue()
    return None


def reproduce_in_template(template_bytes: bytes, *, period,
                          customer_name: str,
                          cell_map: Optional[dict] = None,
                          expected_amount: Optional[float] = None,
                          verify: bool = False) -> Optional[ReproResult]:
    """Fill the operator's template with ONE offtaker's period data + identity and
    render it pixel-perfect. `customer_name` is REQUIRED — the template is reused
    across offtakers, so we must stamp the right bill-to.

    FAIL-CLOSED on identity: writes the name into the metadata CUSTOMER cell, then
    confirms the name actually rendered on the PDF; returns None (caller falls back)
    if the customer cell can't be set or the name doesn't appear — never ships one
    offtaker's invoice under another's name. Returns None when the template can't be
    mapped at all. (Not yet wired into the live send path — needs this safety + review.)"""
    from ..invoice_writer import populate_invoice_workbook
    from .verify import _pdf_lines

    cm = cell_map or build_template_cell_map(template_bytes)
    if not cm or "month" not in (cm.get("field_map") or {}):
        return None

    # Stamp the offtaker's identity into the template (fail closed if we can't).
    stamped = _set_template_identity(template_bytes, cm.get("data_sheet"), customer_name)
    if stamped is None:
        log.warning("reproduce_in_template: could not set bill-to identity — refusing "
                    "to render (would carry the template's sample customer)")
        return None

    sub = types.SimpleNamespace(source_workbook=stamped, parsed_map=cm,
                                customer_name=customer_name)

    def fill(field_map_override):
        return populate_invoice_workbook(sub, period,
                                         field_map_override=field_map_override or cm.get("field_map"))

    def remap(_mismatches):
        r = ai_field_map(template_bytes)
        return r.get("field_map") if r else None

    res = reproduce_invoice(fill, expected_amount=expected_amount, verify=verify, remap=remap)

    # Identity guard: the offtaker's name MUST appear on the rendered invoice.
    if res and res.pdf:
        lines = _pdf_lines(res.pdf)
        nm = (customer_name or "").strip().lower()
        if nm and (lines is None or not any(nm in ln.lower() for ln in lines)):
            log.warning("reproduce_in_template: customer name %r not on the render — "
                        "refusing (wrong-identity guard)", customer_name)
            return None
    return res
