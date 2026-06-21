# AO Audit tab, weekly digest, source-staleness signage, and the FleetStore field-threading trap

Covers the Array Operator "Audit" owner tab, the weekly settlement-audit email,
the per-array "source offline" transparency banner, and the #1 recurring bug
class on the AO frontend: **a new backend field silently dropped in the FleetStore
data layer so it never reaches the card renderer.** Also the slick-PDF brand kit
+ juicy-green Fleet Commander tweak.

## ★ THE RECURRING ROOT CAUSE: fields dropped mid-pipeline (check this FIRST)
When a backend field you added "doesn't show up" on the Arrays tab, it is almost
never the render code — it's that **the Arrays tab does NOT read the backend
fleet-tree directly.** Data flows:

    GET /v1/array-owners/fleet-tree  (backend, api/inverter_fleet.build_fleet_tree)
      → FleetStore.adaptTree(t)      (public/fleet-store.js — tree column → canonical array)
      → FleetStore.toColumns(ids)    (canonical array → sandbox column the renderer reads)
      → sandbox.js render            (reads `col.<field>`)

`adaptTree` AND `toColumns` each REBUILD the object field-by-field and DROP
anything not explicitly listed. So a new column field (`source_status`,
`is_daylight`, `daily`, etc.) must be threaded through **both** functions or it
arrives `undefined` at the card and your `if (col.x...)` guard silently no-ops.
This is the same "logic-layer/field never landed" pattern that hit the GMP
backfill and the reconcile wiring — it recurs because the data path has 3 hops,
each of which can strip a field. **When a field "isn't showing", grep
fleet-store.js for the field name before touching the renderer.**

Verify the full path (not just the renderer) with a Playwright stub that feeds a
mocked fleet-tree JSON through the REAL FleetStore ingest — see
scripts/ao_srcout_verify.py. A DOM-injection hack proves the CSS but NOT the data
path; only routing a payload through adaptTree/toColumns proves the thread.

## Adding a new owner-facing TAB (the Audit tab recipe — 5 wiring points)
Mirrors the Reports/Trends pattern. To add a tab end-to-end:
1. **Backend route** in `api/array_owners.py`: `@router.get("/v1/array-owners/<name>")`,
   auth via `_tenant_from_bearer(authorization)`, iterate the tenant's arrays,
   wrap per-array failures in try/except so one bad array never 500s the whole
   response. Return a `{summary, rows}` shape.
2. **index.html**: nav anchor `<a class="tab" id="tab<Name>" href="#<name>">`,
   a `<section class="panel" id="panel<Name>">` with a root div, and a
   `<link>`/`<script>` include for `<name>.css` / `<name>.js`.
3. **sandbox.js TABS map + tabFromHash + applyView dispatch** (around line ~3547):
   add `<name>: {panel:"panel<Name>", tab:"tab<Name>"}`, the hash case, and the
   `} else if(active==="<name>"){ if(window.__aoLoad<Name>) window.__aoLoad<Name>(); }`
   branch. (Tab switching lives in sandbox.js, NOT app.js.)
4. **<name>.js**: IIFE, reads session from `localStorage.getItem("so_session")`,
   `fetch(API,{headers:{Authorization:"Bearer "+s}})`, handles 401/403 as an
   auth-expired empty state, exposes `window.__aoLoad<Name> = load;`.
5. **<name>.css**: match the site vocabulary — `linear-gradient(165deg,var(--card),var(--card2))`
   panels, 18–22px radii, ambient green glow, `var(--good)/--gold2/--leak`,
   `font-variant-numeric:tabular-nums`. Theme via vars so day/night inherits.

## Honesty rules baked into the Audit surface (Ford's standard)
- Never assert a leak you can't back. The reconcile engine returns `leak` only
  with an INDEPENDENT (inverter) production feed; a GMP-meter-only variance is
  `leak_unconfirmed` — shows the $ but says "connect monitoring to confirm".
- Status-aware variance color: a flagged gap (leak/unconfirmed) must NOT render
  green-positive. leak=red, unconfirmed=gold, clean=green/dim.
- Coverage shown honestly: "N of M auditable" + bills/production-feed counts, so
  a fleet with thin data reads as "needs data", never a wall of fake verdicts.

## Weekly client digest (scheduler pattern)
`api/scheduler.py` → `deliver_weekly_audit_digest()` registered on
`CronTrigger(day_of_week="mon", hour=13, minute=0)` (after the 09:00 reports).
Per active `product=="array_operator"` tenant: build the audit, email
`tenant.contact_email` via `notify._send_via_resend(..., product="array_operator")`
with `email_skin.render_email_skin(product="array_operator")`. Sends an "all
clear" when nothing's flagged (not silence), skips tenants with no bills to audit
(no noise), per-tenant try/except so one fleet can't stall the batch. Verify the
rendered email by sending a REAL Resend message to ford.genereaux@gmail.com (set
`notify.RESEND_API_KEY` from ~/.hermes/secrets/resend_api_key inside the script).

## Source-staleness transparency ("data outage at the SOURCE, not us")
Backend `inverter_fleet._source_status(inv_rows)` → `{state: ok|stale|none,
last_report, age_hours}` from the freshest inverter `last_report`; `stale` =
older than `_SOURCE_STALE_HOURS` (6h). Added to each column as `source_status`.
Threaded through fleet-store (see the trap above). Card renders, when stale:
a `⚠ SOURCE OFFLINE` corner ribbon + amber glowing card frame
(`.sb-array--srcout`) + an in-card banner naming the vendor + age, framed as
"data outage at the source — not Array Operator. Live data resumes when <vendor>
reconnects." Ford wants this UNMISSABLE (three signals), and the framing must put
the outage on the vendor, never on AO.

DIAGNOSING "no live data" on Arrays: it's usually NOT our bug. SolarEdge
`/site/{id}/overview` returns `currentPower` + `lastUpdateTime` + `lastDayData`;
when a site stops sending data to SolarEdge, ALL of these go stale/zero (confirmed
sites frozen 13–33h with currentPower=0 AND today_Wh=0). Our pipeline reads it
correctly; the source is dark. Other compounding facts: all SE sites often share
ONE api_key → the budget governor (`api/poller.py` `_governor_allows`,
`DAILY_BUDGET_PER_KEY=280`) spaces each site to ~once/51min; and only a handful
of arrays have any live-capable connection at all (most are GMP-billed, no
inverter API). Probe live state with railway-ssh python: run `poll_all_sources(force_daylight=True)`
and read its `{arrays_polled, arrays_skipped, arrays_throttled, readings_written}`
summary + the freshest `inverter_readings.ts`.

## Slick PDF brand kit + juicy-green Fleet Commander
- `api/billing/_pdf_brand.py` is the ONE source of truth for AO PDFs: palette
  (from styles.css :root), the dark "energy" hero band (brand mark + title +
  right-aligned headline + footer, painted on the canvas), and the green-gradient
  energy bar chart as a reportlab Flowable. `invoice.py` + `summary.py` both import
  it. No new deps. Quarterly = a cadence, reuses invoice+summary (no separate gen).
  Render to PNG with pymupdf (`fitz`) + vision_analyze to QA; never fabricate
  empty-data charts — show an honest "no data yet" state.
- "Muted green" Fleet Commander fix: the OK state greens were ~0.6 opacity under a
  frosted plate. Juicy = brighter `.fc-tank--ok` gradient (rgba 34,210,110/.96 →
  82,245,158/.86) + inner glow + `.fc-card.ok` green border + outer glow. NOTE the
  OK styling only applies at healthyPct>=95 (the green state); 85–94 is `warn`
  (amber) by design — to preview the green, force a 95%+ ok state.

## Extension content-script "no console output" → make every gate LOUD
When a capture content script (e.g. `extension/solarweb_content.js` Fronius) is
"producing nothing in the console", the cause is almost always a SILENT
early-return at a gate — `tick()` returns with no log when there's no intent
flag, not signed in, or `captureFlow()` threw into a `catch(_){}`. The user is
then debugging blind. FIX THE CLASS: add a `LOG()` at EVERY gate so the console
narrates exactly where it stops:
- on load (top of the IIFE): `LOG("content script loaded vX.Y.Z on", host)` —
  **absence of this line = the extension isn't injected on that tab** (the single
  most common real cause; tells the user to reload with the extension enabled).
- per tick: `intent: yes/NO`, `signed in: yes/NO`, `captured systems: N — inverters: N`.
- replace swallowing `catch(_){}` around the capture with `catch(e){ LOG("capture failed:", e.message) }`.
- a `✓ capture complete` confirmation on success.
The intent gate (`hasIntent()`) means capture only runs if the user clicked
"Connect <vendor>" in Array Operator within ~10 min THEN went to the portal —
order matters; if they hit the portal first, intent is unset and it no-ops.
DELIVERY: the extension is loaded MANUALLY in Chrome and is NOT auto-deployed —
a `git push` does nothing for a packaged-build user. Bump the manifest version
FIRST, then `bash scripts/build_extension_zip.sh` (drops the zip on Ford's C:
Desktop, never OneDrive), and VERIFY the zip actually contains the new version +
your new strings (`unzip` + `grep`) before handing it over. "Load unpacked" needs
the unzipped folder, not the .zip. ASK how they run it (packaged vs unpacked)
before deciding whether to rebuild.

## Stat-card spacing pitfall (reportlab Table)
A 3-row stat card (eyebrow/value/sub) with only 2px padding crowds the value and
sub-line. Give explicit `BOTTOMPADDING` under the eyebrow (~7) and under the value
(~6) + leading on the value row for comfortable rhythm. Ford flags cramped rows.
