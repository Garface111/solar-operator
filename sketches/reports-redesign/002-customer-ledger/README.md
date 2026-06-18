# 002 — The Customer Ledger (customer-centric)

A redesign sketch for the **Reports** tab of Array Operator, the billing tool Paul
Bozuwa uses to invoice his **four** solar customers each month.

## Design stance

**The customer is the organizing unit.** The screen is a calm list of Paul's four
customers as persistent cards — and that same list is *also* where he manages them.
There is no separate "customers" admin screen and no "new run" wizard: the ledger
is the home base. Paul glances at it, sees who's ready, edits a % if a contract
changed, and sends. Manage and send live in one surface because, at four
customers, splitting them apart only adds clicks.

Mental model: **a ledger of stable accounts**, not a CRM and not a batch job.

## Key choices

- **Customer cards are the home base.** Each row shows everything Paul needs at a
  glance: name, linked array, fixed share %, email, last invoice (date + amount),
  and current-period status (Ready / Check % / Sent).
- **Manage where you send.** Expanding a card reveals an inline **Edit customer
  details** form (name, array, email, and a **% slider**). Editing the slider live
  recomputes the customer's dollar share against this period's kWh — Paul sees the
  money move before he saves. Landowner remainder (100 − %) updates with it.
- **Add customer is first-class and always visible.** A dashed "+ Add a customer"
  row sits at the bottom of the list and expands **inline** (name / email / array /
  % slider) — no hidden modal. New customers join the ledger immediately.
- **Per-customer audit trail.** Each expanded card shows that customer's invoice
  **history** — period, the `generation × % = allocated kWh` math, the amount, and a
  PDF link. The math is always visible so Paul can verify it.
- **Never auto-send.** Every current-period draft shows the full calculation and a
  **Review & send** button. Sending is an explicit act; the hint reminds him the
  GMP invoice PDF is attached and the email is drafted, not auto-fired.
- **Durable records.** Sending writes the invoice into that customer's history and
  flips status to "Sent," mirroring real persistence.
- **One real state transition + guard.** "Norwich Union Village" opens in a
  **needs-attention** state (amber, contract % renews this month, send disabled).
  Confirming the % clears the warning, turns the card green, and unlocks sending —
  and `Send all ready (N)` recounts live.

## Trade-offs

- **Customer-first, not period-first.** Great when accounts are few and stable.
  If Paul scaled to dozens of customers, a period-centric "this month's run" view
  would scan faster; here, four cards fit on one screen, so customer-first wins.
- **Density vs. calm.** Cards stay airy and glanceable, which means the detailed
  math lives one click deep (inside the expanded panel) rather than always on
  screen. Deliberate: the home base should feel like a control panel, not a ledger
  printout.
- **Mixed responsibilities per card.** Each card does management *and* sending. For
  4 stable customers this is a feature (one place); at larger scale it would blur
  concerns.

## Best for

Paul exactly as he is today: a **tiny, stable roster** where the customers
themselves — not the billing period — are the thing he returns to. Maximum control
and auditability with minimal clicks, and a single home base for both managing and
invoicing.
