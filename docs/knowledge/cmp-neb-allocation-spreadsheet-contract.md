# Central Maine Power (CMP) — NEB allocation spreadsheet (grounded Jul 2026)

**Source:** Ford screenshot `Screenshot 2026-07-13 130623.png`  
(path: `OneDrive/Pictures/Screenshots 1/`, 1572×393 Excel view, opened as CSV so Excel warned about possible data loss).

**Purpose of this note:** ground what a real CMP *Net Energy Billing (NEB)* offtaker/allocation workbook looks like so Cloud Capture, offtaker import, and invoice math can map onto it — not invent a second schema.

---

## What this file is

This is **not** a raw portal usage dump. It is CMP’s (or the project host’s export of) **Net Energy Billing allocation ledger** for one facility month:

- One **facility / project** contract
- Many **subscriber (offtaker) rows** that share that facility’s generation
- Per-subscriber: allocation %, rate category, kWh applied, $ billed, banked kWh

This is the **billing truth surface** for community / NEB solar under CMP — closer to our offtaker subscription model than to daily AMI intervals.

---

## Header / columns (left → right, as shown)

| Col | Header (as labeled) | Meaning / notes |
|-----|---------------------|-----------------|
| A | **NEB Family** | Project/family id (e.g. `7000038435`) — groups all offtakers on one facility |
| B | **Facility Contract Account** | Facility-side CMP contract account (e.g. `30012850605`) — **shared** across offtaker rows |
| C | **Program Type** | Always `NEB Tariff Rate` in this sheet (Maine NEB program) |
| D | **Billing Portion** | Integer portion / tier index (values 4–14 in sample) |
| E | **Billing Key Date** | Period key as `Mon-YY` (here all `Feb-26`) |
| F | **Facility Generation kWh** | **Facility total** for the period — only filled on **one** lead row (`48,862`); other offtaker rows leave it blank |
| G | **Applied to Current Month Invoices** | kWh applied into this month’s invoices — lead row `48,861` (≈ full facility gen) |
| H | **Small Price** | Small-system / tier rate (lead shows `0.204101`; many offtaker rows blank or `0`) |
| I | **Medium Price** | **Primary $ / kWh rate** for most offtakers — sample `0.202185` |
| J | **Large Price** | Large-tier rate (mostly `0` in sample) |
| K | **Active** | `Y` / `N` — whether the offtaker is active on the project |
| L | **Name** | Offtaker / subscriber display name (often truncated): `WISCASSET C…`, `UNALLOCATE…`, `TRI POND VA…`, `HERRING BR…`, `COUSINEAU…`, `KVCAP ENER…`, `MAINE STREE…`, `EDUCARE CE…` |
| M | **Subscriber C…** | Scientific-notation subscriber/customer id (e.g. `3.0011E+10`) — CMP internal account key |
| N | **Percentage** | **Allocation % of facility** (e.g. `5.4278`, `47.092`, `13.7227`). Sum of active offtakers should ≈ 100% minus unallocated |
| O | **Priority** | Queue / bank priority (sample mostly `0`) |
| P | **Rate Catego…** | Rate class label: `SGSPPrimary?`, `StreetLightin…`, `MGSSeconda…`, `MGSPrimary` |
| Q | **kWh Applied** | This offtaker’s kWh this period (e.g. `2,652`, `23,010`, `6,705`) |
| R | **Current Mon…** | **$ amount** this month (e.g. `$536.22`, `$4,652.30`, `$1,355.69`) |
| S | **kWh Banked** | Banked generation credit (sample mostly `$ -` / empty) |
| T | **Current Mon…** | Related current-month bank column (mostly empty / `$ -`) |
| U | **Used Bank k…** | Bank draw this period |
| V | **Used Bank $** | $ value of bank used |
| W | **Expired W…** | Expired bank (partial header) |

Selected cell in screenshot: **I4 = `0.202185`** (Medium Price for the UNALLOCATE / street-light row).

---

## Structural facts (load-bearing)

1. **One facility, many offtakers**  
   Same `NEB Family` + same `Facility Contract Account` on every data row. Our model maps:
   - Facility → `Array` / project
   - Each named active row → `BillingReportSubscription` (offtaker)
   - `Percentage` → `allocation_pct` (sheet is 0–100 style, not 0–1)

2. **Facility generation is on one row only**  
   Do **not** sum column F across offtakers — only the lead/aggregate row carries `Facility Generation kWh`. Offtaker kWh lives in **kWh Applied** (col Q).

3. **Rate is offtaker-specific**  
   Medium Price ~`$0.202185/kWh` is common; Small Price `0.204101` appears on the lead row. Invoice math should use the **row’s** price, not a single array default, when this sheet is the source of truth.

4. **UNALLOCATE row is real**  
   Name like `UNALLOCATE…` with StreetLighting rate and its own % / kWh / $ — residual / host share, not a customer offtaker. Import should flag or map to “host / unallocated”, not invent a fake client email.

5. **Period is month-grain (`Feb-26`)**  
   CMP NEB billing here is **monthly**, not daily AMI. Daily Cloud Capture from the portal is still useful for production dashboards; **this sheet is the invoice allocation source**.

6. **Dollars ≈ kWh × rate** (sanity)  
   Example: `2,652 kWh × 0.202185 ≈ $536.22` — matches the screenshot. Good cross-check for import.

7. **Exported as CSV from Excel**  
   Banner: “Possible Data Loss… comma-delimited (.csv)”. Real operator workflow may hand us `.csv` or `.xlsx` of this shape. Offtaker import / generation spreadsheet tracker should recognize these headers.

---

## Mapping to EnergyAgent models

| Sheet concept | Our field / surface |
|---------------|---------------------|
| NEB Family | Project / array external id (store on Array or template) |
| Facility Contract Account | Utility account # on the **facility** meter |
| Name | `customer_name` |
| Percentage | `allocation_pct` (÷ 100 if stored 0–1) |
| Medium/Small/Large Price | `net_rate_per_kwh` / offtaker rate override |
| kWh Applied | Period offtaker kWh (invoice line) |
| Current Mon $ | Invoice amount check |
| Billing Key Date | Period label (`2026-02`) |
| Active = N | Pause / exclude offtaker |
| UNALLOCATE | Host residual — not a normal offtaker send |

---

## Implications for Cloud Capture (`cmp` provider)

We already ship `api/harvester/vendors/cmp.py` for **portal login + usage sniff**. This screenshot adds:

1. **Portal capture alone is incomplete for NEB billing** — operators also live in this allocation export. Prioritize:
   - Import path for NEB CSV/XLSX (headers above)
   - Optional: if portal exposes “NEB allocation” JSON, sniff for `Percentage` / `Facility Generation` / subscriber ids
2. **Facility contract account** `30012850605` is the account number to match on utility meter capture / bill attach for the **array**, not each offtaker’s retail account (if different).
3. **Rate schedule labels** (`MGSSecondary`, `MGSPrimary`, street lighting) matter for Maine rate lookup later — store raw `Rate Category` on the offtaker when importing.

---

## Sample offtakers visible in the shot (for QA fixtures)

Approximate rows (Feb-26, family `7000038435`, facility `30012850605`):

| Name (trunc.) | % | kWh Applied | $ | Notes |
|---------------|---|-------------|---|--------|
| (lead / facility) | — | Facility gen 48,862 | — | F+G filled |
| UNALLOCATE… | ~5.43 | 2,652 | $536.22 | Street lighting / residual |
| TRI POND VA… | ~4.24 | 2,071 | $418.72 | |
| HERRING BR… | ~1.28 | 624 | $126.17 | Multiple Herring rows |
| COUSINEAU… | ~47.09 | 23,010 | $4,652.30 | Largest share |
| KVCAP ENER… | ~6.37 | 3,106 | $625.97 | |
| MAINE STREE… | ~1.26 | 615 | $124.41 | |
| EDUCARE CE… | ~13.72 | 6,705 | $1,355.69 | |

(Use as fixture seeds only; re-OCR/full export before production import.)

---

## Follow-ups (when Ford wants them)

1. Offtaker import template column aliases for CMP NEB headers.  
2. Detect NEB Family + Facility Contract Account on upload → auto-create array + offtakers.  
3. Cross-check invoice draft: `kWh Applied × Medium Price` vs computed amount.  
4. Portal HAR for CMP NEB pages — see if the same table is available as JSON after login.

---

## Related code

- Cloud Capture vendor: `api/harvester/vendors/cmp.py`
- Provider catalog: `api/data/providers/ME.csv` → `cmp` live
- Offtaker billing: `api/billing/` allocation + invoice_ledger
- Screenshot artifact copy: keep original in Ford’s Screenshots folder; optional repo-side sample under `docs/samples/` only if Ford wants it committed (PII/names — prefer not to commit customer names without OK)
