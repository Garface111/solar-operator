# Array Operator frontend (vanilla-JS) — recurring bug patterns & fixes

The Array Operator OWNER site (`/root/array-operator/public/`) is hand-rolled
vanilla JS (no framework). Deploy is manual to Netlify — see the
`solar-operator-deploy` skill §5b (CLI auth wall + `scripts/netlify_api_deploy.py`).
These are bugs that have actually bitten, with the exact fix.

## 1. Frontend/backend body-shape mismatch → HTTP 422 "Couldn't save"
Symptom: a save button shows "Couldn't save (HTTP 422) — try again." 422 = the
backend Pydantic model rejected the request BODY shape; it is almost never auth
(that's 401) or a server bug.

Real case (Master Account): the "Company" field POSTed `{company_name: val}` to
`/v1/account/company-name`, but the backend model `UpdateCompanyName` expects
`{name}`. Key mismatch → 422. Fix was one line on the client: send `{name: val}`.

DIAGNOSIS RECIPE (do this, don't guess):
1. Find the fetch in the frontend; note the exact `body` keys it sends.
2. Find the endpoint's Pydantic model in the backend (`api/account.py` etc.) and
   read its field names. `search_files` for `class Update...`.
3. They won't match — align the client to the model (prefer fixing the client;
   the endpoint contract is shared/stable). DON'T loosen the backend model.
4. PROVE it against the running backend before claiming fixed:
   old shape → 422, new shape → 200 (or 401 with a bogus token, which still
   means the body PARSED). A 401 with a fake token is the success signal that the
   shape is right.

Gotcha while testing email/identity fields: `EmailStr` rejects reserved TLDs like
`.test` ("special-use or reserved name") with a 422 that looks like your bug but
isn't — test with a real `.com` address.

## 2. `order: 0` sort key is falsy — view registers in the wrong slot
The Trends chart-view registry sorts with `(a.order || 99)`. Passing `order: 0`
to make a view sort FIRST silently becomes 99 (0 is falsy) and it lands LAST.
Use `order: 0.5` (or any positive number below the others) to put a view first.
Same trap anywhere code does `x || default` on a numeric field that legitimately
takes 0. Symptom: "I set order 0 but it's not first."

## 3. CSS class display overrides the `[hidden]` attribute
`.foo{display:flex}` BEATS the UA `[hidden]{display:none}` rule (class selector
specificity > attribute), so setting `el.hidden = true` / `setAttribute("hidden")`
in JS does NOT hide the element — it stays `display:flex`. Symptom seen: the
Master Account password editor was permanently expanded, so the "Current password"
field showed even on accounts that had never set a password (a logical
contradiction the user spotted). Fix: add an explicit `.foo[hidden]{display:none}`
rule alongside the base rule. Audit any JS-toggled `hidden` element whose base
rule sets `display:` to something other than the default.

## 4. Sitewide LABEL renames — user-facing strings ONLY
Ford asks for renames as the product's terminology evolves ("rename X to Y
sitewide"). He means USER-FACING LABELS ONLY: display text, headings, button
labels, placeholders, error messages, AND the customer-facing PDF/xlsx invoice
line labels (`api/billing/invoice.py`). NEVER touch internal field names, API
keys, element IDs, `data-*` attributes, function names, or variables — that
breaks the contract for zero benefit. Confirmed renames: "net rate" → "solar
credit rate"; "customer(s)" → "offtaker(s)" (kept `customer_name`, `has_customers`,
`#rbqCustomer`, `data-sub="customers"`, `renderCustomers` all intact). State the
internal/label split back to him when doing a rename.

## 5. Remove a whole nav TAB cleanly (don't leave orphans)
To remove a tab from index.html SPA-style nav, hit ALL of: (a) the `<a class="tab"
id="tabX">` nav button, (b) the `<section class="panel" id="panelX">`, (c) the
`<link>`/`<script>` includes for that tab's css/js, (d) the routing in
`sandbox.js` — the `TABS` map entry, the `tabFromHash()` branch, and the
`applyView()` `else if(active==="x")` branch. Then delete the now-orphaned
js/css files. Verify `#x` falls back to the default tab and the live bundle has
zero refs. Scope check: a "Claims tab" removal is JUST the tab — leave in-Arrays
claim-drafting + marketing copy unless explicitly asked (confirm scope).

## 5b. CONSOLIDATE a tab/surface into another WITHOUT losing functionality
Ford asks to merge surfaces ("delete the Offtakers tab, do it all through the
Invoice Generator — maintain functionality"). The hard part is the "maintain
functionality" constraint: the two surfaces usually edit DIFFERENT fields. Recipe:
1. AUDIT BOTH surfaces first — read the render fn + the save fn of each and list
   exactly which fields/actions each one owns. (Here: the Offtakers tab edited
   name/email/CC/array/share%/rate via `saveCustCard`; the Invoice row's Edit
   panel only had delivery/send-to/discount. The merge had to fold the former in.)
2. FOLD the unique fields into the survivor, REUSING the existing save logic —
   don't rewrite it. `saveCustCard(card)` keys off `[data-f="..."]` inputs +
   `.rb-cust-status`, so dropping those same `data-f` inputs into the survivor's
   panel let the identical function work unchanged. Pass any extra data the new
   host needs (e.g. `subCard(s, arrs)` now receives the array list for the
   dropdown; `refreshList` fetches it alongside the subscriptions).
3. RELOCATE entry points — the "＋ Add an offtaker" button + its manual-add form
   mount (`MANUAL_HOST_ID`/`MANUAL_AFTER_ADD`) moved from the deleted tab into the
   survivor's list header, wired in `load()`.
4. DELETE the dead code: the removed surface's render/card/wire fns, its nav
   button, panel div, and routing branches. KEEP shared helpers the survivor now
   calls (kept `saveCustCard`, deleted `renderCustomers`/`custCard`/`wireCustCard`).
5. PROVE persistence end-to-end, not just that it renders: drive Playwright to
   edit a field in the new location, click save, then read the value back from
   the backend API. Revert the test value. ("It renders" ≠ "it saves".)
Also: grep the bundle for dangling refs to everything removed
(`renderCustomers|rbSubCustomers|data-sub="customers"`) before committing.

## 5c. OVERDESIGN AUDIT method (Ford asks "audit for functionality + overdesign")
Render every state live (Playwright + vision_analyze each subtab), THEN separate
two verdicts: FUNCTIONALITY (works? real data? console errors? — usually solid)
vs OVERDESIGN (too many always-on controls, redundancy, decorative-in-a-document).
Concrete overdesign smells found + fixes this session:
- A list ROW with ~11 always-on controls per item → collapse to a compact summary
  (name + status chips + 1 rate line) + 2-3 primary buttons + an "Edit ⌄" toggle
  that reveals a `.rb-sub-more` (start `hidden`; toggle handler does local
  show/hide and MUST `return` before any `refreshList()` that would rebuild+collapse
  the row). With N items the old way is a wall.
- The SAME number shown 3x (a chip + a derived line + an editable input) → keep
  ONE representation (the line + the input), drop the chip.
- A DECORATIVE chart (Solar Spiral) inside a customer-facing report → remove it;
  keep only the chart that conveys real content (the daily-generation bars). Also
  drop the now-dead fetch/cache it required.
- Redundant eyebrow PILLS that just echo the heading/subtab name → delete the
  eyebrow, keep the `<h3>`.
Before unilaterally cutting taste-level items on a tab Ford calls "almost there",
offer the trim set via clarify and let him pick — but DO make the clear wins.
Caveat surfaced: a "your default" provenance badge next to an override is NOT a
bug — verify suspected data bugs against the API before "fixing" (the discount
override resolved correctly; the badge was rate-SOURCE provenance, separate).

## 5d. STACKED-COLUMN trends + per-array filter (power-user upgrades)
- "Show all visualizations at once, no tabbing": replace the switcher with all
  views rendered into per-view hosts down a column; `teardown()` loops
  `_activeStops[]` (every mounted view's stop fn), not a single one. Caption
  multi-year-only art (liquid/spiral/heatfield) honestly when there's <2 years of
  data ("needs 2+ years") so a near-empty chart reads as building-history, not broken.
- Per-array FILTER: add an optional `?array_id=N` query param to `/fleet-trends`
  that scopes the AGGREGATES (monthly_by_year, daily_recent, ttm, lifetime,
  savings) to one owned array, but keep `by_array` FULL-FLEET (so the dropdown can
  switch) and echo `selected_array_id`; unowned id → 404. Frontend keeps the full
  array list across scopes (`_fleetArrays`) so a scoped empty array shows an inline
  empty state WITH the dropdown still present (never strands the user).
- GMP data must be UNIONED into trends: fleet-trends merges
  `gmp_daily_generation` (via the read contract) with the CSV `DailyGeneration`
  table per array per day, CSV winning on overlap (no double-count). Trends/the
  bar graph read `DailyGeneration` only by default — they were blind to GMP data
  until this union.

## 6. Local QA harness for this site
Backend `uvicorn api.app:app :8788` (from /root/solar-operator venv +
`SOLAR_DATA_DIR` sqlite, seed via `seed_probe.py` which mints a token to
/tmp/ao_token.txt) + `dev_proxy.py 8089` from /root/array-operator (mirrors the
Netlify /v1 proxy). Set `localStorage so_session` = the token, load
`http://127.0.0.1:8089/index.html#<tab>`. Recurring traps: uvicorn started before
an edit serves STALE code (kill+restart, no --reload); local backend OOM (exit
137) reads as "Session expired" in the UI; the session token EXPIRES mid-Playwright
(re-mint before each shot run); dev_proxy returns 501 on PUT/PATCH so test those
verbs directly against :8788. Always Playwright-screenshot + vision_analyze every
UI change before claiming done.
