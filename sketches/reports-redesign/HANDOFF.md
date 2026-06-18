# HANDOFF — Reports tab redesign (Hybrid "Billing Run + Manage")

**Visual target:** `sketches/reports-redesign/004-hybrid/index.html` (open in a browser).
Screenshots: default billing-run state + manage-mode state (see chat).
**Picked direction:** Variant 1 (period-batch billing run) as the spine + Variant 2's
customer management folded in as a "Manage customers" mode on the same surface.

**Why a handoff, not a direct build:** `web/app/src/screens/ReportsTab.tsx` is shared
and actively evolved across work streams. This spec lets whoever owns that file
re-layout it without a parallel fork. Do NOT create a second ReportsTab.

---

## The good news: the backend already exists

Almost everything the mockup needs is already built and DEPLOYED (commits 441f086 +
03f0cb4). This is a FRONTEND RE-LAYOUT over existing endpoints, not new backend work.

### Endpoints (all under `/v1/array-operator/billing`, verified in api/billing/routes.py)
| UI element (mockup) | Endpoint | Frontend helper (api.ts) |
|---|---|---|
| Customer rows in the run table | `GET /subscriptions` | `listBillingSubscriptions()` (line 1416) |
| "Add a customer" form (manual) | `POST /subscriptions` (no file; customer_name+array_id+allocation_pct) | `createManualSubscription()` (line 1437) |
| Array dropdown in add form | — | `listAllArrays()` (line 1479) |
| Inline % edit commit | `PATCH /subscriptions/{id}` | (add `allocation_pct` to SubscriptionPatch — see GAP 1) |
| Per-row "Review" drawer | `POST /subscriptions/{id}/draft` then `GET /drafts` | (wire — see GAP 2) |
| Attach GMP PDF in drawer | `POST /drafts/{draft_id}/gmp-invoice` | exists |
| Edit draft email before send | `PATCH /drafts/{draft_id}` | exists |
| "Approve & send" | `POST /drafts/{draft_id}/approve` | exists |
| "Review & send all N" (batch) | loop approve over ready drafts | (compose client-side — see GAP 3) |
| History (past periods) | sent drafts / `last_sent_at` on subs | (read from existing fields) |

### Data contract per customer row (already in `_sub_dict`)
`{ id, customer_name, array_id, allocation_pct (0..1), client_email, delivery_mode,
   cadence, last_sent_at, last_invoice_number }`
The math in the UI = `array_period_generation × allocation_pct × credit_rate`. The
period generation + computed share already come back from the draft/preview path
(`build_manual_match` in delivery.py computes `allocation_pct × array period kWh`).

---

## The layout to build (replace the NEPOOL-quarter scaffolding)

The current ReportsTab is built on QuarterCard / "ship status" — that's the NEPOOL
verifier mental model and is WRONG for an Array Operator owner like Paul. Replace the
body with:

1. **Hero "Current billing run" panel** (period label + month + "GMP generation
   posted <date>" + period total + primary `Review & send all N →`). One card.
2. **Run table** — one row per customer subscription: name + array + editable %
   pill, the inline math (`gen × % = kWh × $rate`), customer-share $, a status chip
   (Draft / Needs GMP PDF / Ready / Sent), and a row action (Review / Attach GMP PDF).
3. **"Add a customer" dashed row** under the table → expands inline to the manual
   form (name, array `<select>` from listAllArrays, % number, email) → POSTs
   createManualSubscription → refetch list. (This is the form that already exists in
   AddCustomerCard.tsx — relocate/restyle it INTO the table, don't keep it as a
   separate card.)
4. **"Manage customers" mode toggle** — when on: show the blue manage banner, enable
   the % pills as editable, surface the add form, and DIM/disable the send controls
   (status chips, row actions, AND the top "Review & send all" button). When off:
   the billing-run send flow.
5. **Review drawer** (right slide-over) — calc breakdown card, both PDF attachments
   (customer invoice + GMP), editable To/Subject/Message, "Approve & send". Footer:
   "Nothing sends automatically."
6. **History section** below — collapsed period rows, each expands to its sent
   invoices with per-line math + Invoice/GMP PDF links. The durable audit trail.

### CSS / theme
Match the mockup, which already uses the app tokens: bg `#fafafa`, cards `#fff`,
primary `#047857` (emerald-800) + `#34d399`, amber `#f59e0b` for "Needs GMP PDF",
radius ~10px, system font, tabular-nums on all kWh/$ figures. The mockup's inline
`<style>` is a drop-in reference for the Tailwind classes (most map to existing
`primary-*`, `amber-*`, `emerald-*` utilities already in tailwind.config.js).

---

## Backend GAPS to close (small, called out loudly)

**GAP 1 — % editable via PATCH.** `SubscriptionPatch` (routes.py line ~298) does NOT
include `allocation_pct` or `array_id`. Add both as `Optional[float]` / `Optional[int]`
to the pydantic model + apply them in `patch_subscription`, so the inline % edit
persists. Backend one-liner; mirror the existing field handling.

**GAP 2 — "draft this period" trigger.** The run table needs a per-customer "Review"
that creates (or fetches) the current-period draft. `POST /subscriptions/{id}/draft`
exists (line 524) and builds a ReportDraft from the manual allocation. The frontend
just needs a helper that calls it then opens the drawer with the returned draft.

**GAP 3 — batch send.** No single batch endpoint. Compose client-side: gather all
subs whose draft status is "ready", call `POST /drafts/{id}/approve` for each, show
the progress bar. (A future `POST /drafts/approve-batch` would be cleaner but isn't
required for v1.)

**GAP 4 — the manual draft needs a real period generation number.** `build_manual_match`
uses the array's most recent period generation (DailyGeneration → Bill fallback).
Paul said HE builds the GMP-invoice detection that supplies the authoritative period
generation; until then the manual path's number is "best available from our data" —
surface it as such in the drawer (don't imply it's the GMP-posted figure if it isn't).

---

## Hard requirements / pitfalls already paid for
- **Never auto-send.** Every path ends at a human "Approve & send". The drawer footer
  must say so. (Paul's #1 requirement.)
- **Show the math, always.** `gen × % = kWh × rate = $` inline on every row + in the
  drawer. Auditability is the product.
- **Remainder = landowner.** When %s on one array sum < 100%, the remainder is the
  landowner's — the add form hints this. Validate per-array sum ≤ 100% (warn, don't
  hard-block; Paul may add customers across periods).
- **Durability.** Every sent run is a saved record (ReportDraft status=sent +
  sub.last_sent_at/last_invoice_number). History reads from these — don't recompute.
- **Don't break the xlsx path.** The upload-workbook subscription still works; the
  manual path is additive. The redesigned tab should offer BOTH ("Add a customer"
  manual + an "Import workbook" affordance), manual as the default for a 4-customer
  operator.

## Deliverable placement
Mockup + this handoff live in `sketches/reports-redesign/`. When the owning agent
picks this up, the AddCustomerCard.tsx already shipped is the seed of the add form —
restyle/relocate it into the new table layout rather than keeping it as a bolt-on card.
