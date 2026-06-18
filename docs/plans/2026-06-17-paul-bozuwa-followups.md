# Paul Bozuwa — Onboarding Demo Follow-ups (captured 2026-06-17)

Source: `Paul_s Onboarding Summary.txt` (demo call, Wed Jun 17 2026).
Status: **CAPTURED, NOT BUILDING YET.** This is a tracked backlog of Paul's
requests + action items so nothing is lost. Prioritize/scope before building.

Paul is a real prospect (4 customers, VT arrays on Fronius + Chint, GMP utility).
The demo validated signup → Fronius connect → multi-account (distinct arrays, no
dupes) → dashboard cards/weather/"open in Fronius" → warranty-claim drafting →
report ingestion. The asks below are what would convert him.

---

## Feature requests (ours to build)

### 1. Per-customer percentage-allocation invoicing  ⭐ the thing he's paying for
Workflow Paul described:
1. GMP posts an invoice → detect it, extract **total generation** for the array.
   - **Paul will build the GMP-invoice DETECTION backend himself.** Our piece
     starts from "here is the array's total generation for the period."
2. Apply each customer's **fixed % allocation** (from Paul's spreadsheet).
   - Allocations are arbitrary per customer/array. Real example: one array splits
     **95% to a customer / 5% to the landowner**. Model must support N customers
     per array with per-customer percentages (sum ≤ 100%, remainder = landowner).
3. Produce a **customer invoice PDF** + attach the **GMP invoice PDF**.
4. Send a **drafted email for Paul's approval** (not auto-send).
5. **Persist** each run — append a new spreadsheet row OR write to a trusted DB.
- Reuses our existing billing/report/PDF/drafted-email stack (see
  ARRAY_OPERATOR_BILLING.md). The NEW part is the allocation engine + the
  per-customer/array percentage data model.
- Scale: small — **4 customers**. Keep the customer list simple.

### 2. Trailing-12-month + seasonal YoY reporting + macro multi-year trend tab
- Trailing-12-month production comparison.
- Seasonal **year-over-year** comparison (same season across years).
- A **macro-level tab** with multi-year trend lines across arrays.
- Data backbone already exists (`DailyGeneration` history) — this is a reporting
  VIEW, low risk. Likely the best pure-win runner-up after invoicing.

### 3. Auditable, reproducible spreadsheet templates
- AI reads Paul's invoice spreadsheet templates, reproduces their EXACT format,
  populates with fresh data — output must be **auditable** (he can verify the math).
- Preserve generated data as a **new spreadsheet row** or migrate to a **trusted
  database** (he wants durability, not overwrite-in-place).

---

## Data gaps (Paul's action items — NOT ours, but track)

- **Chint (Paul's account): no data since 2024.** Paul believes it's a physical
  site issue (fuse/hardware/wiring) → raising with **Runtime Solar**.
  - ⚠️ OPEN QUESTION for us: confirm our Chint capture isn't ALSO contributing
    before assuming it's purely hardware. (Deferred — Paul is escalating first.)
- **Norwich co-owned Fronius**: Paul lacks credentials → requesting from
  **Norwich Technologies**. Will add that array once he has them.
- Paul confirmed multi-account Fronius works: separate logins → distinct arrays,
  no duplicates. (Validates our tenant/dedup work.)

## Paul's action items (his, tracked for follow-up)
- [ ] Contact Runtime Solar re: Chint no-data-since-2024.
- [ ] Request Norwich Fronius credentials from Norwich Technologies.
- [ ] Add missing arrays to dashboard once credentialed.
- [ ] Build his GMP-invoice-detection backend (feeds our allocation engine #1).

## Open product questions raised in the demo
- Extension UX: Paul asked how it knew the Danville site + credentials — a future
  **explicit install step** is planned (reduce the "how did it know?" surprise).
- "Save schedule" semantics for spreadsheet reports — clarify in UI.
- "Can I add more arrays now?" — yes; confirm the self-serve add-array path is
  obvious post-signup.

---

## Suggested build order (when greenlit)
1. **#1 allocation invoicing** — highest leverage; it's what he's paying for and
   reuses the billing stack. Start with the data model (customer ↔ array ↔ %)
   then the PDF + drafted-email, fed by a manual/Paul-supplied generation total.
2. **#2 YoY/seasonal/macro reporting** — pure win, data already there.
3. **#3 template reproduction + durable storage** — overlaps #1's persistence.
