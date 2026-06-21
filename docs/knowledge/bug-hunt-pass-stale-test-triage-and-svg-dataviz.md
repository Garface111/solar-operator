# Bug-hunt pass, stale-test triage, and the AO SVG dataviz pattern

Two reusable playbooks that came out of an open-ended "find bugs and fix them" pass plus a
"make the graphs bars not lines" change. Both recur across this codebase.

## A) "Find bugs in the system" — the ordered hunt (don't poke randomly)

When Ford says "find bugs and fix them," work this order — earliest signal wins:

1. **Prod error logs first.** `railway logs 2>&1 | grep -iE "Traceback|Error| 500 |Exception|IntegrityError|psycopg2"`.
   Real users hitting real failures right now beats speculation. (Often clean — that's a good sign, move on.)
   Ford BLOCKS interactive prod-endpoint curls but `railway logs` (real traceback) + `railway ssh ... python`
   (read-only SQL) are allowed and fastest.
2. **The test suite is the bug radar.** Run it. But FIRST it may not even COLLECT — see §B.
   `DATABASE_URL=sqlite:///./test.db python -m pytest -q` (NEVER inherit a prod-pointed DATABASE_URL —
   MC's suite has autouse delete fixtures; this codebase is safer but keep the habit).
   Run WITHOUT `-x` once you've cleared collection so you see ALL failures at once, not one-at-a-time.
3. **Grep the known recurring bug-classes proactively** even if no test caught them. The big one here:
   `uq_array_per_tenant` soft-delete name-collision → 500 (full detail in
   `references/capture-endpoints-array-matching-and-offtaker-bill-binding.md` PITFALL 1). This session it
   was found a THIRD time: `solaredge_connect_account` + `locus_connect_account` built their name-collision
   guard (`if name in names_lower`) from LIVE arrays only, so a site colliding with a soft-deleted array's
   name slipped past disambiguation → INSERT → `IntegrityError` 500. Fix: a SEPARATE `all_names_lower` set
   from ALL names (no `deleted_at` filter) just for the guard; keep reuse maps on live arrays. Grep:
   `search_files "deleted_at.is_(None)"` near any `db.add(Array(` / array name-map build.
4. **Prove a real bug with a failing-then-passing regression test.** Write the test, `git stash` the fix,
   run it → confirm it reproduces the exact error (`UNIQUE constraint failed`), `git stash pop`, run → passes.
   That stash-prove step is what makes "I fixed a real bug" trustworthy vs. a test that always passed.
5. **Deploy + verify route health**: push → wait SUCCESS → curl affected routes for **401/422 (loads)** vs
   **500 (broken)**. 422 just means more required fields; both mean the module imports & routes fine.

## B) Stale-test vs real-bug triage (a test failing ≠ a product bug)

Most test failures in this repo are STALE TESTS asserting old behavior, not regressions. Triage rule:
**read the product code's intent (esp. a CRITICAL comment) before "fixing" anything.** If the code is
deliberately doing X and the test asserts not-X, the TEST is wrong. Concrete cases seen:

- **Collection-crashing ImportError from untracked WIP.** 3 test modules imported models
  (`WeatherLocation`, `SolarEdgeFetchRaw`, …) that were never added to `api/models.py`. The modules +
  their adapters/jobs are **git-untracked local WIP** (the "never committed → never happened" pattern;
  `git ls-files` returns nothing, `git status` shows `??`). A hard `ImportError` at import time crashes
  pytest COLLECTION for the WHOLE suite, blinding the gate. FIX (do NOT delete a co-agent's WIP): make the
  module skip cleanly — `pytest.importorskip("api.adapters.x")` and/or wrap the model import in
  `try/except ImportError: pytest.skip("… not present yet", allow_module_level=True)`. Pyright will flag the
  unknown symbols — that's expected, it's exactly why we skip.
- **Branding/domain drift.** `test_branding` asserted the stale `solaroperator.org` fallback; AO now sends
  from its OWN verified domain `arrayoperator.com` (confirmed in `branding.from_address` docstring). Fix the test.
- **Consumption-vs-generation semantics (VEC/SmartHub).** Tests asserted bill kWh → `kwh_generated`, but the
  sync path has a long CRITICAL comment: SmartHub/VEC bill kWh is **CONSUMPTION** → `kwh_consumed`, NEVER
  `kwh_generated` (writing it to generation zeroed every VEC/WEC NEPOOL report). Tests were asserting the old
  BUG. Fix the assertions to `kwh_consumed == N` and `kwh_generated is None`.
- **Time-fragile fixtures.** `test_inverter_fleet` hardcoded a past `last_report` timestamp; `_live_power_w`
  correctly DROPS a vendor live reading older than `_SOURCE_STALE_HOURS` (the SOURCE-OFFLINE honesty gate), so
  the test went stale with the calendar. Fix: make the fixture timestamp relative —
  `(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()`.

Ford prizes honesty here: a stale test corrected with a comment explaining WHY beats forcing the product to
match an outdated assertion. Always name whether each failure was a real bug or a stale test in the summary.

## C) AO inverter/production graphs = SVG dataviz pattern (sandbox.js)

The Arrays view has THREE daily-kWh production graphs, all hand-rolled inline SVG (no chart lib), all reading
`Math.max(0, +d.kwh || 0)` over a `daily` series. When changing graph STYLE, change ALL THREE or the view
looks inconsistent (Ford notices mismatched UI):
1. `invSpark(daily, statusCls)` — per-inverter card mini-graph (w132 h34).
2. `_renderArraySeries(byDate, label, stroke, emptyMsg)` — combined array graph (w300 h56), fed by
   `arrayGraph()` which sums per-inverter daily or falls back to the array/utility split series.
3. `tileSpark(col, tone)` — zoomed-out fleet-tile minigraph (w100 h26).

Shared conventions to PRESERVE on any restyle:
- Health tone: `bad→var(--bad)`, `warn→var(--warn)/#ffb454`, ok→`var(--good)`, idle→`var(--faint)`;
  **utility stream → blue `var(--util,#5b8def)`**. Don't flatten these to one color.
- Zero-output days render a **red baseline dot** (dead-streak signal) — keep it.
- Time axis (`_sparkTimeLabel`: ISO→"M/D", demo "d-N"→"now"/"Nd") and the kWh/MWh total stay.
- `sleep` state has a CSS dim rule; when you swap element types (e.g. `polyline`→`rect`), ADD the matching
  selector (`.sb-inv.sleep .sb-inv-spark rect{fill:#5a6790!important;opacity:.6}`) or sleeping cards stop dimming.

**Bar-chart recipe** (line/area → bars): per series of `vals`, `slot=(w-2*pad)/vals.length`,
`gap=min(k, slot*0.2)`, `bw=max(min, slot-gap)`, each bar `<rect x y width height rx>` with
`bh=max(min,(v/max)*(h-2*pad))`, zero → the red `<circle>` at baseline instead of a rect. Drop the
`<polygon area>`+`<polyline>` pair.

**Visual QA before deploy is MANDATORY for any UI change** (Ford's hard rule). You can't screenshot the live
app pre-deploy, so build a tiny standalone `/tmp/.../index.html` that inlines the changed render functions with
sample data, `python3 -m http.server`, drive Playwright headless to screenshot, and `vision_analyze` it. Confirm
bars render cleanly (no overlap/clipping), tones correct, zero-dots present — THEN deploy. AO deploy is always
`python3 scripts/netlify_api_deploy.py` then commit+push; verify live with `curl .../sandbox.js | grep -c "rect x="`
(bars present) and `grep -c "polyline points"` (== 0). Then tell Ford to hard-refresh (Cmd/Ctrl+Shift+R).
