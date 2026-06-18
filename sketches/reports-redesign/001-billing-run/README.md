# Reports Redesign — Variant 001: "The Billing Run" (period-centric)

A throwaway, self-contained HTML mockup (single file, inline CSS/JS, no build step) for the
redesigned **Reports** tab in Array Operator — the solar billing tool used by Paul Bozuwa to
invoice his **4** customers every billing period.

## Design stance

The organizing unit is the **billing period**, not the customer. Paul's real job is a once-a-month
*batch*: GMP posts generation → apply each customer's fixed % allocation → produce invoices →
review the drafted emails → **approve & send** (never auto-send) → keep a durable record.

So the screen is built as a **billing run**:

- **Hero "This period" panel** at the top = the current open period (May 2026) with all 4 customers
  as rows, each ready to review and send in a single pass.
- A prominent **"Review & send all 4 →"** batch button plus a progress bar (`0 of 4 sent`).
- Below the hero, a **collapsed history timeline** of past runs — the durable audit trail. Each
  period expands to its sent invoices with PDF links.

This is a **control panel he glances at**, not a CRM. He lands here once a month, sees the 4
invoices waiting, and clears them.

## Key choices

- **Inline math on every row.** `1,284 kWh × 95% = 1,220 kWh`, then `× $0.1485/kWh` → the dollar
  amount. Paul can verify generation × % = customer share at a glance — auditability is a feature,
  not a drill-down.
- **Status chips drive the flow.** `Needs GMP PDF` (amber) → `Draft` (blue) → `Ready` (green) →
  `Sent` (grey). Chips are clickable to advance state; the amber state has its own
  "Attach GMP PDF" action because that's the one real blocker.
- **Per-row Review drawer** slides in with the editable email draft (To / Subject / Message),
  both PDF attachments, a calculation breakdown card, and **Approve & send**. Nothing sends
  automatically — the foot note says so.
- **Batch send** only fires invoices that are `Ready`, and warns if some aren't — preserving
  control while minimizing clicks on the happy path.
- **History = durability.** Past periods are saved records, collapsed by default, each expandable
  to audit the exact math and re-download invoice + GMP PDFs.

## Interactivity (real state transitions)

- Click **Attach GMP PDF** on Norwich → row goes amber → `Ready`.
- Click a **status chip** to advance `Needs → Draft → Ready`.
- **Review** opens the drawer; **Approve & send** flips that row to `Sent`, advances the progress
  bar, and toasts confirmation.
- **Review & send all 4** batch-sends every `Ready` invoice at once; button locks to "All sent ✓"
  when the run is complete.

## Trade-offs

- **Period-centric, so a single customer is a row, not a page.** Great for the monthly batch;
  less ideal if Paul wanted a deep per-customer history view (that lives in History expansion here).
- The hero assumes one open period at a time. If GMP posts late and two periods overlap, this
  layout would need a period switcher (out of scope for the mockup).
- Clickable chips double as both status display and a manual override — powerful but slightly
  unconventional; a tooltip hints at it.

## Best for

Paul's actual cadence: **a fast, low-click monthly billing pass** where the whole run is visible at
once, the math is auditable inline, sending is always deliberate, and every period is a durable
record. Strongest when the customer count stays small and the work is inherently batched.

## Fake data

- Customers: Danville Big Buck Solar (95%), River Road Community (90%), Norwich Union Village (88%),
  Green Mountain Dairy (97%).
- Blended net-metering credit rate: $0.1485/kWh. Period: May 2026. History: Feb–Apr 2026.
