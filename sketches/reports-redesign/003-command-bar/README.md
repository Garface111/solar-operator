# Reports Redesign — Variant 003: "The Command Bar"

**Stance:** Action-first / speed. The most opinionated, lowest-click take on the Reports tab.
**Mental model:** Linear / Superhuman for solar billing. A pro tool Paul blasts through in ~60 seconds, once a month.

> Live mockup: `index.html` — single self-contained file, inline CSS + vanilla JS, no build step.

---

## The job this screen serves

Paul Bozuwa runs a tiny solar operation: **exactly 4 customers**. Every billing period he must:

1. Take each array's generation from the GMP utility portal
2. Apply each customer's **fixed % allocation** (e.g. Danville = 95% customer / 5% landowner)
3. Produce a customer invoice PDF + attach the GMP statement PDF
4. Review/edit a drafted email
5. **Approve & SEND** (never auto-send)
6. Have every run persisted as a durable, auditable record

The scale is tiny and the cadence is monthly. The bet of this variant: Paul doesn't want a dashboard to *explore* — he wants a **fast lane** to clear this month's invoices with maximum control and minimum clicks.

---

## Key design choices

- **One dense table IS the screen.** 4 rows × 7 columns (Customer · Array · Generation · Alloc % · Customer $ · Status · Action). No cards, no wizard, no multi-step flow. Everything Paul needs to decide is on one line.
- **A single smart command bar** above the table: a big primary **"Review & send 3 ready"** button (with an `R` shortcut) plus **segmented filter pills** (All / Ready / Sent / Needs PDF) that actually filter the visible rows.
- **Inline % editing.** The allocation cell is a live editable field — click, type, Enter. The Customer $ and the running total recalculate instantly (constant credit rate, fully recomputed). No modal to change a number.
- **One-click per-row status action.** Each row's action button is contextual: *Review & send* (green) when ready, *Attach GMP PDF* (dashed amber) when a PDF is missing, *Sent ✓* when done. The status chip and the action always agree.
- **Right slide-over review panel.** Opening a row slides a panel in from the right with: generation/allocation/total stats, the two PDF attachments (invoice + GMP), and the **editable draft email** (to / subject / body). Nothing sends until **Approve & send**.
- **Keyboard-forward, and the UI says so.** `j`/`k` move the row cursor, `Enter` opens, `Cmd/Ctrl+Enter` sends, `Esc` closes. After a send, the panel auto-advances to the next ready customer so Paul can chain all sends without touching the mouse. The hints are printed in the command bar and the panel footer — discoverable, not hidden.
- **Sticky running-total footer.** Always-visible bar: **"$2,747 across 3 invoices this period"**, a 6-period sparkline, ready/needs-PDF/sent counts, and a "All runs persisted · last saved" durability/audit cue. The total updates live as % values change or invoices send.
- **History is a single link.** "Past periods" is a small dropdown affordance in the header, not a section. This variant bets Paul mostly cares about the **current run** and wants it fast; audit history is one click away when he needs it.

## Visual language

Light, cool, sharp. White table on `#f7f8f8`, solar-green `#047857` primary actions, lighter `#34d399` accents, amber `#f59e0b` for "needs PDF", subtle zinc grid lines, system font stack, small type, tight rows. Minimal chrome, maximum signal.

---

## Trade-offs

**What this variant wins**
- Fastest possible path from "open tab" to "all invoices sent." Likely the lowest click-count of any variant.
- The whole period's state is legible at a glance — totals, what's ready, what's blocked.
- Power-user ergonomics (keyboard chaining, inline edit) reward the operator who does this every month.

**What it gives up**
- **Discoverability.** A first-time or infrequent user sees a dense grid and shortcut hints; there's no guided walkthrough. The speed payoff assumes Paul learns the tool.
- **History is deliberately demoted.** If auditability/browsing past runs turns out to be a frequent job (not just a safety net), this layout under-serves it.
- **Density doesn't scale visually.** Perfect for 4 rows; a single flat table would get unwieldy at 40+ customers (out of scope here — Paul has 4).
- **Inline editing has less room for context/validation** than a dedicated edit screen would (e.g. explaining *why* an allocation is what it is).

## Best for

An operator who **already knows the routine**, values **control + minimum clicks**, runs this on a **predictable monthly cadence at tiny scale**, and would rather feel like they're flying a cockpit than filling out a form. If the priority were onboarding, exploration, or heavy historical auditing, a calmer/guided variant would fit better.

---

## Interactions implemented (all real, vanilla JS)

- Segmented filter pills filter visible rows + sync their counts.
- Inline % edit → live recalculation of Customer $ and the sticky footer total + a confirmation toast.
- Row click / "Review & send" / `R` / `Enter` opens the right slide-over with that customer's draft + PDFs.
- Editable draft email (to / subject / body), per-customer pre-filled.
- "Needs PDF" rows show a missing-attachment state and disable Approve & send until resolved.
- **Approve & send** (button or `Cmd/Ctrl+Enter`) marks the row Sent, shows an audit-log confirmation, updates the footer, and auto-advances to the next ready customer.
- `j`/`k` cursor movement, `Esc` to close, slide-over up/down nav buttons.

## Verification

Screenshot-verified with headless Playwright (Chromium, viewport 1280×1400) in three states — default table, "Needs PDF" filter, and the open slide-over review panel. Confirmed programmatically: 4 rows render, no console/page errors, no horizontal overflow, filters resolve to correct counts, slide-over fits the viewport exactly (x=740, w=540), `Cmd+Enter` transitions a row to Sent, and inline % edit recomputes the dollar amount. Screenshots in `/tmp/shots003/`.
