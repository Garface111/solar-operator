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


def reproduce_in_template(template_bytes: bytes, *, period,
                          customer_name: Optional[str] = None,
                          cell_map: Optional[dict] = None,
                          expected_amount: Optional[float] = None,
                          verify: bool = False) -> Optional[ReproResult]:
    """Fill the operator's template with one offtaker's period data and render it
    pixel-perfect. `period` is a dict/Period the fill understands (array_kwh,
    customer_kwh, tariff, adder, dates, month). Pass cached `cell_map` to skip the
    mapping step. Returns a ReproResult (ok None/True/False), or None when the
    template can't be mapped at all."""
    from ..invoice_writer import populate_invoice_workbook

    cm = cell_map or build_template_cell_map(template_bytes)
    if not cm or "month" not in (cm.get("field_map") or {}):
        return None

    sub = types.SimpleNamespace(
        source_workbook=template_bytes,
        parsed_map=cm,
        customer_name=(customer_name or (cm.get("customer") or {}).get("name") or "Offtaker"),
    )

    def fill(field_map_override):
        fm = field_map_override or cm.get("field_map")
        return populate_invoice_workbook(sub, period, field_map_override=fm)

    def remap(_mismatches):
        r = ai_field_map(template_bytes)
        return r.get("field_map") if r else None

    return reproduce_invoice(fill, expected_amount=expected_amount,
                             verify=verify, remap=remap)
