"""Single source of truth for the ARRAY OPERATOR *generation-reports* plan.

THE FOLD (Jul 2026): NEPOOL Operator became Array Operator's Invoices ->
"Generation reports" subtab. This is the billing UNIT for that capability — the
operator uses Array Operator to auto-generate + deliver NEPOOL/REC generation
workbooks to each of their reporting clients.

Pricing model (Ford, Jul 2026 — FINAL): **$15.00 per client per calendar QUARTER,
charged on the FIRST real OUTPUT for that (client, quarter), then unlimited.** An
"output" = a report SEND (auto or manual) OR a DOWNLOAD of the deliverable workbook
(per-client or the all-clients directory). Building + previewing + auto-propagating
the whole fleet is FREE; the $15 fires only when the operator actually takes a
deliverable for a client-quarter. Every subsequent output for that same
(client, quarter) is free; a DIFFERENT quarter is a fresh $15.

  first output for a (client, quarter)   $15.00   (then unlimited that quarter)

  3 clients, one quarter, each output once or ten times -> $45
  same 3 clients, next quarter                          -> another $45

Mechanics (see api/delivery.py + api/jobs/genreports_usage.py):
  * Each first output writes a GenReportCharge ledger row, idempotent via
    UNIQUE(tenant_id, client_id, quarter) — that uniqueness IS the "then unlimited".
  * A METERED Stripe price (usage_type='metered', unit_amount=1500) receives one
    usage unit per un-pushed ledger row, summed per tenant per billing period.

Stripe: mint the metered price with scripts/create_ao_genreports_price.py, point
STRIPE_AO_GENREPORTS_PRICE_ID at it. INERT UNTIL ACTIVATED: no usage is pushed and
no line bills until that env var is set (the usage job + the subscription-line
helper both guard on it).
"""
from __future__ import annotations

# The flat billing unit, whole cents. $15.00 per client per quarter (per the first
# output). This plan is in whole dollars.
PRICE_CENTS: int = 1_500

# Readable alias.
PER_CLIENT_CENTS: int = PRICE_CENTS


def compute_monthly_cents(billable_units: int | None) -> int:
    """Total cents for N billable client-quarter units at the flat $15 unit.

    A "billable unit" is one (client, quarter) that has had its first output — i.e.
    one GenReportCharge row. 0 for units <= 0.

      0 -> 0    1 -> 1500 ($15)    3 -> 4500 ($45)    10 -> 15000 ($150)
    """
    n = int(billable_units or 0)
    if n <= 0:
        return 0
    return n * PRICE_CENTS
