# Handoff: make our GMCS workbook match Crown REC's numbers

**Audience:** the agent picking up the "our reports don't match Crown" fix.
**Status:** diagnosed (root cause strongly evidenced, not yet confirmed on prod
data). Read-only diagnostic is written and validated. No fix applied yet.
**Owner context:** Ford (ford.genereaux@gmail.com). His father Bruce manages
Green Mountain Community Solar (tenant `ten_14b76982523a3b47`, the live pilot).
Crown REC Services (John Spencer) is the REC agent that uploads to NEPOOL-GIS.

Follow the repo rules in `CLAUDE.md` — especially: **do NOT break the GMCS
writer format rules**, flag uncertainty LOUDLY, and **verify the cause on real
prod data before coding the fix** (Ford explicitly approves READ-ONLY prod
queries for this; he trust-checks output).

---

## 1. What's wrong

Ford compared our automated workbook (`GMCS_NEPOOL_Q2_2026.xlsx`) against the
`GMCS.xlsx` Crown sent Bruce. Three gaps, in priority order:

### (A) Monthly generation VALUES don't match — the important one
Every array, every month differs from Crown. Measured on the 4 arrays present in
both files (Chester, Tannery Brook, Timberworks, Waterford):

| | our Q1'26 (Jan/Feb/Mar) | Crown Q1'26 | per-month RECs ours→Crown |
|---|---|---|---|
| Chester | 15.93 / 15.55 / 19.84 | 12.61 / 16.92 / 21.06 | 15,15,19 → 12,16,21 |
| Tannery Brook | 1.30 / 4.20 / 10.68 | 1.34 / 1.92 / 13.37 | 1,4,10 → 1,1,13 |
| Timberworks | 4.18 / 9.13 / 14.87 | 4.04 / 7.55 / 15.52 | 4,9,14 → 4,7,15 |
| Waterford | 5.07 / 9.97 / 16.26 | 4.54 / 8.80 / 18.42 | 5,9,16 → 4,8,18 |

Pattern: **quarter TOTALS are close (~1–4%), but the split across the three
months is off by up to ~5 MWh.** Our workbook's within-quarter month spread is
~20% SMALLER than Crown's (measured: ours 7.03 vs Crown 8.76 MWh mean spread).
That flattening is the fingerprint of **calendar-day bill proration**
(`api/bill_attribution.distribute_kwh_by_calendar_day`): a billing-period total
is spread evenly across the days of the period, so a bill that straddles two
months moves generation between them and dilutes the real monthly peaks
(clearest in winter/shoulder months where daily output swings hardest).

**Why it matters:** RECs are floored per month, so a different monthly split =
different monthly REC counts = a report that won't reconcile line-by-line with
what Crown submits to NEPOOL-GIS. This is a data-integrity issue, not cosmetic.

### (B) Title is missing the NEPOOL-GIS ID
Crown: `Chester (53984)`. Ours: `Chester`. The writer already renders
`"<name> (<id>)"` when `Array.nepool_gis_id` is set — it's just **NULL in our DB**
for these arrays. Known IDs from Crown's file:

| array | nepool_gis_id |
|---|---|
| Chester | 53984 |
| Tannery Brook | 46425 |
| Timberworks | 61959 |
| Waterford | 78671 |

Get the FULL set from Bruce/Crown before backfilling (only 4 are known here).

### (C) Window length: 6 quarters vs Crown's 7
Crown shows 7 trailing quarters (Q3'24 → Q1'26); our default is 6. Note the
*end* quarter is already correct: `default_reporting_reference_date()` now ends
the window on the NEPOOL minting quarter (Q1 2026 in July 2026), matching Crown —
see the recently-merged lag fix. Only the COUNT differs. Confirm with Bruce that
Crown always uses 7 before changing the default (`quarters=6` in
`gmcs_writer.build_workbook` / `report_has_data`); judging from one file.

---

## 2. Root-cause hypothesis (confirm on prod before fixing)

`build_workbook` sources each month as `{**bill_months, **daily_months}` — i.e.
**DailyGeneration wins when present, else it falls back to prorated bills.**
Hypothesis: the diverging arrays are **bill-only** (no daily coverage), so they
get prorated (smoothed) values, while any daily-backed array should already
match Crown closely.

This was reproduced synthetically and behaves exactly as predicted: a Feb–Mar
bill of 30 MWh prorated to Feb 18.39 / Mar 11.61 (Crown: 16.92 / 21.06 — real
March is higher), while a daily-backed month matched Crown within **0.11 MWh**.

---

## 3. Run the diagnostic (READ-ONLY, on prod)

`scripts/diag_crown_parity.py` reproduces `build_workbook`'s EXACT per-month
sourcing and, for the 4 known arrays, prints the delta vs Crown split by source
(daily vs bill). It writes nothing. It embeds Crown's ground-truth values.

The script is NOT deployed — pipe it into the Railway container over stdin
(pattern from `docs/knowledge/ao-deploy-and-frontend-debugging.md` §19):

```
railway ssh "cat > /app/diag_crown_parity.py && cd /app && \
    PYTHONPATH=/app python diag_crown_parity.py --tenant ten_14b76982523a3b47; \
    rm -f /app/diag_crown_parity.py" < scripts/diag_crown_parity.py
```

(Plain `python /tmp/x.py` fails `ModuleNotFoundError: api` — you MUST write into
`/app` and set `PYTHONPATH=/app`.)

**Read the output like this:**
- `bill_mo` / `daily_mo` / `none_mo` per array = how many window months come from
  each source. Lots of `bill_mo` on the diverging arrays confirms the fallback.
- The per-month parity table + the INTERPRETATION footer compute mean |Δ| vs
  Crown for **bill-sourced** vs **daily-sourced** months:
  - bill Δ large **and** daily Δ small → confirmed: proration is the cause →
    fix = feed true daily/monthly generation (§4).
  - BOTH large → our underlying kWh source itself differs from Crown's meter
    reads → this is a DATA-SOURCE question for Bruce, not a proration fix.
    STOP and escalate; don't silently rewrite the writer.
- `none_mo` (no data at all for a month) is a coverage gap — those months render
  blank in the real workbook and are a separate backfill problem.

---

## 4. Candidate fixes (decide AFTER the diagnostic; lay out tradeoffs to Ford)

Ordered by accuracy. Do not pick blind — the diagnostic tells you which applies.

1. **Feed true monthly generation instead of prorated bills** (correct fix if
   the diagnostic confirms proration).
   - GMP exposes **daily** usage/generation; if we capture it into
     `DailyGeneration` for these arrays, the writer already prefers it per-month
     and the numbers converge (proven: 0.11 MWh in the synthetic test). Check
     whether the GMP adapter/daily-backfill (`api/jobs/gmp_daily_backfill.py`,
     `api/reports/*_read.py`) already pulls daily for these accounts and why it's
     absent for the diverging arrays (never ran? account not linked? parser?).
   - Speed/cost/accuracy: highest accuracy; effort = ensure daily capture runs +
     backfills history. This is the durable fix and matches Bruce's meter.

2. **Improve bill→month attribution** (partial mitigation if daily is
   unavailable for some arrays). Calendar-day proration assumes flat daily
   output; weighting by a solar-insolation curve would reduce the winter error.
   More accurate than flat proration, still an approximation — flag as such.

3. **Get Crown's actual source** (if the diagnostic shows BOTH sources diverge).
   Crown's numbers may come from revenue-grade NEPOOL-GIS meter data we don't
   currently ingest. That's a Bruce conversation about the authoritative source,
   not a writer change.

Whatever you do, **re-run the diagnostic after the fix** and confirm the 4
arrays match Crown within a tight tolerance (daily-backed months hit ~0.1 MWh).
Add/extend tests near `tests/test_writer_daily_generation_takes_precedence.py`.

---

## 5. The two smaller parity items

- **(B) NEPOOL-GIS IDs:** once Bruce supplies the full list, backfill
  `Array.nepool_gis_id` (prod update via `railway ssh`, verify with
  `inspect`/a read-back). The writer needs no change. Titles then read
  `<name> (<id>)` automatically.
- **(C) 7 quarters:** if Bruce confirms, change the `quarters` default 6→7 in
  `build_workbook` and `report_has_data` (keep them equal — they must agree or
  skip-if-empty desyncs from generation). Footnote row auto-reflows
  (`foot_row = 31 if row <= 31 else row`). Add a test asserting 7 quarter blocks.

---

## 6. Guardrails

- READ-ONLY on prod until Ford approves a write. Backfills (IDs) are writes —
  confirm first.
- Do NOT break the GMCS format rules in `CLAUDE.md` (A1:C1 merge, row-5 header
  size 14, verbatim footnote, RECs = floor(MWh), col widths 24 — the 24-vs-13
  width difference from Crown is Ford's DELIBERATE choice, leave it).
- Test with non-Bruce data where possible.
- The `GMCS_NEPOOL_Q2_2026.xlsx` Ford has is the PRE-FIX artifact (30 sheets incl.
  14 non-producing, ends Q2). The non-producing-exclusion and Q1-lag fixes are
  already merged; a freshly generated workbook already drops the empties and
  ends on Q1 2026. Only (A) values, (B) IDs, (C) 7-quarters remain.
