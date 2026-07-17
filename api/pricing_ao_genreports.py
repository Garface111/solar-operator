"""Single source of truth for the ARRAY OPERATOR *generation-reports* plan.

THE FOLD (Jul 2026): NEPOOL Operator became Array Operator's Invoices ->
"Generation reports" subtab. This is the billing UNIT for that capability — the
operator uses Array Operator to auto-generate + deliver NEPOOL/REC generation
workbooks to each of their reporting clients.

Pricing model (Ford, Jul 2026 — FINAL): **$15.00 per ARRAY per calendar QUARTER,
charged on the FIRST real OUTPUT covering that (array, quarter), then unlimited.** An
"output" = a report SEND (auto or manual) OR a DOWNLOAD of the deliverable workbook
(per-client or the all-clients directory). Building + previewing + auto-propagating
the whole fleet is FREE; the $15 fires only when the operator actually takes a
deliverable. Every subsequent output covering that same (array, quarter) is free; a
DIFFERENT quarter is a fresh $15.

The UNIT IS THE ARRAY, not the client (Ford corrected this 2026-07-16, before any
price was minted): we bill exactly the arrays that RENDER in the workbook
(writers.gmcs_writer.reported_array_ids — non-excluded, non-deleted, actually
producing in the window). A force-hidden or non-producing array never bills.

  first output for an (array, quarter)   $15.00   (then unlimited that quarter)

  a 5-array client, one quarter, output once or ten times -> $75
  the same 5 arrays, next quarter                         -> another $75
  3 clients of 2 / 3 / 5 arrays, one quarter              -> $150

Mechanics (see api/delivery.py + api/jobs/genreports_usage.py):
  * Each first output writes one GenReportCharge ledger row PER REPORTED ARRAY,
    idempotent via UNIQUE(tenant_id, array_id, quarter) — that uniqueness IS the
    "then unlimited".
  * A METERED Stripe price (usage_type='metered', unit_amount=1500) receives one
    usage unit per un-pushed ledger row, summed per tenant per billing period.

Stripe: mint the metered price with scripts/create_ao_genreports_price.py, point
STRIPE_AO_GENREPORTS_PRICE_ID at it. INERT UNTIL ACTIVATED: no usage is pushed and
no line bills until that env var is set (the usage job + the subscription-line
helper both guard on it).
"""
from __future__ import annotations

# The flat billing unit, whole cents. $15.00 per ARRAY per quarter (per the first
# output covering it). This plan is in whole dollars.
PRICE_CENTS: int = 1_500

# Readable alias — the unit is one reported ARRAY for one quarter.
PER_ARRAY_CENTS: int = PRICE_CENTS


def compute_monthly_cents(billable_units: int | None) -> int:
    """Total cents for N billable array-quarter units at the flat $15 unit.

    A "billable unit" is one (array, quarter) that has had its first output — i.e.
    one GenReportCharge row. 0 for units <= 0.

      0 -> 0    1 -> 1500 ($15)    3 -> 4500 ($45)    10 -> 15000 ($150)
    """
    n = int(billable_units or 0)
    if n <= 0:
        return 0
    return n * PRICE_CENTS
