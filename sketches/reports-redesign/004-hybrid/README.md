## Variant: Hybrid — Billing Run + Customer Management (PICKED)

### Design stance
Variant 1's period-batch "Billing Run" is the spine (matches Paul's monthly rhythm:
land once, review & send all customers in one pass, leave). Variant 2's customer
management is folded IN — not as a separate screen but as a "Manage customers" mode
layered on the same table: inline-editable allocation %, an always-available
"Add a customer" row, and a manage banner that pauses sending while he edits.

### Key choices
- **One screen, two modes.** Default = the billing run (send invoices). "Manage
  customers" toggle = edit %s / add customers. Same table, no context switch.
- **The math is always visible** (gen × % = kWh × rate = $) — Paul's auditability
  requirement, shown inline on every row and in the review drawer.
- **Inline % edit** is a click-to-edit pill on each row; committing re-computes the
  dollar share live with a confirming toast.
- **Add customer** is a first-class dashed row under the table (expands inline, no
  modal), surfaced automatically in manage mode.
- **History below** = the durable audit trail; every sent run is a saved record,
  expandable to per-invoice math + PDF links.
- **Batch send** ("Review & send all N") + per-row Review drawer (editable draft
  email + both PDFs + Approve & send). Nothing auto-sends.

### Trade-offs
- Strong at: matches Paul's actual monthly job; full control (send + manage) with
  minimum clicks; auditable; durable.
- Weak at: two modes on one surface needs a clear visual mode-switch (handled with
  the banner + dimming send controls in manage mode). One nit: the top "Send all"
  button should also dim/disable in manage mode (fix in implementation).

### Best for
A small, relationship-driven operator (Paul: 4 stable customers) who bills monthly
on a fixed % split, reviews before sending, and needs to prove the math.

### State
Throwaway mockup, fake data. Picked direction. Next step is the drop-in integration
spec in HANDOFF.md against the real web/app/src/screens/ReportsTab.tsx + the billing
API already built (POST /v1/array-operator/billing/subscriptions manual path,
allocation_pct/array_id columns). Do NOT fork ReportsTab — hand off the spec.
