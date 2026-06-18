# Aggregate State — Solar Operator / Array Operator (2026-06-18)

Unifies FOUR concurrent work streams this session: 3 other agents + the
settlement-auditing/certification venture. Deduped, with cross-stream
interactions reconciled. Source of truth for "where is everything."

## The four streams
- **A — Data-hub scaling:** rewrote the poller for fleet scale + SMA runbook + alert investigation.
- **B — Array Operator billing/reports:** re-add fixes, $4k bill bug, watchdog, Trends tab, Reports approval inbox, per-customer delivery mode.
- **C — Data-hub build + sponge:** original 5-min poller (Phase 1), GMP full-history "data sponge," fluid-sim spike.
- **D — Settlement auditing → revenue certification:** reconciliation engine (merged, inert), leak thesis falsified on GMP, pivot to certification, buyer roster + Encore pitch.

═══════════════════════════════════════════
SHIPPED & LIVE ON PROD (arrayoperator.com / Railway)
═══════════════════════════════════════════
Billing / data integrity (B):
- 3 re-add upsert fixes (the recurring SELECT-then-INSERT 500 class — all 3 unsafe writes in the endpoint now upserts).
- "$4k/month bill" = a single corrupt 677,533 kWh Fronius row (94% of total) → corrected to ~$253/mo. Pricing ($0.005/kWh) was fine.
- Plausibility guard (nameplate×24h ceiling) at BOTH ingest write paths; full corrupt-data sweep (both tables, all tenants → zero); daily 03:45 UTC billing-safety watchdog (alerts before the 04:00 usage report).

Live telemetry / poller:
- (C) Phase-1 5-min server-side poller + InverterReading time-series table + daily prune; `_live_power_w` honesty gate (no stale-capture-as-live-at-night).
- (A) Poller REWRITTEN for scale: ONE site-level call per site (vendor-agnostic; turns on SMA/Fronius/Locus/AlsoEnergy the instant creds land), per-API-key budget governor (≤280 calls/day, adaptive cadence, cannot blow SolarEdge's 300/day). 16 new tests. Deploy 4217c4a7 SUCCESS, health 200.

Data sponge (C):
- Bill model extended w/ full energy fields + `raw_json` column (store the WHOLE bill, lose nothing). absorb_history() w/ live SpongeProgress, fired on GMP capture. Proven: 2,924 bills / 16.4 yrs (back to 2010) for one tenant. Parser re-derived from stored raw_json with ZERO re-pulls → 2,908 w/ cost, avg 15.47¢/kWh (matches Bruce's real economics).

Frontend (B+C, merged):
- Trends tab (portfolio multi-year line chart + seasonal YoY + per-array) on the Array Operator site. (B caught it was first built in the retired NEPOOL Arrays tab and rebuilt it in the right app; fixed a misleading -47% partial-year YoY → honest +5.6%.)
- Reports approval inbox: draft → review → approve & send, with GMP PDF attach (ReportDraft model + 5 endpoints, approval-inbox UI).
- Per-customer delivery_mode (approval default / auto-send) with scheduler routing + toggles.
- Energy-history view + SpongeProgressCard ("Importing your N years…"), reusing the SAME TrendsView/MultiYearLineChart components so the two agents' work reads as one product.
- Fronius freshness window 3h→24h (B) so capture-only cards don't blank between captures.

Fronius freshness window 3h→24h (B) so capture-only cards don't blank between captures.

Settlement-audit engine (D):
- `api/reconciliation/` (classify + reconcile + ReconResult) committed b1a786a, pushed to main, deployed, health 200, both golden fixtures pass on deployed code. INERT — nothing imports it yet; zero behavior change.

═══════════════════════════════════════════
BUILT BUT INERT / PARKED
═══════════════════════════════════════════
- (D) Reconciliation engine — deployed but un-wired. Callable, verified, safe. Wiring into gmcs_writer.py:310 + alerts + onboarding = specs build2/build3 (depend on coordinating with A/C who own those files).
- (C) WebGL fluid-sim spike — VALIDATED ("3 tanks · 1 draw call"), parked in /root/array-operator/spikes/001-webgl-fluid-tank/, ready to productionize.

═══════════════════════════════════════════
SPEC'D, NOT BUILT
═══════════════════════════════════════════
- (D) build2-alert-pipeline-spec.md — monthly variance→$→alert + VT credit-expiry clock.
- (D) build3-coverage-onboarding-spec.md — per-array audit-readiness gate.
- (A) 3–4×/day shared fault scan feeding both inverter-alerts + warranty (Ford said no — not built).
- (D) host-meter-boundary-fix-spec.md — group-array reconciliation (implemented in the engine already).

═══════════════════════════════════════════
CROSS-STREAM INTERACTIONS (the part the individual reports miss)
═══════════════════════════════════════════
1. POLLER: C built it, A rewrote it. C's original 1-inventory+N-per-inverter path (~63 calls/site) is the EXACT expensive path A removed. Consistent, not conflicting — A superseded C's implementation. Current poller = A's site-level + governor version.
2. _POWER_FRESH 3h→24h is a DOUBLE-EDGED edit. B widened it to stop Fronius cards blanking. C found that same widening CAUSED the Tannery Brook "SMA producing at 9 PM" bug, and fixed it via the `_live_power_w` night gate (NOT by reverting the window). So: window stays 24h (B's fix intact) AND night-honesty holds (C's gate). Both needed; don't revert either without the other.
3. The expensive 1+N SolarEdge path A killed in the poller STILL runs in the inverter-alert/warranty detection path (A's Task 3 finding) — at fleet scale it competes with the live poller for the API budget. A's proposed shared-scan fix is the consolidation, parked by your call.
4. WEB ACCESS: A reported web research down (Firecrawl) and asked you to run `hermes model`. THIS IS NOW PARTLY RESOLVED — D wired in your BYOK Firecrawl key (chmod600 ~/.hermes/secrets/firecrawl_api_key, set in ~/.hermes/.env). Direct Firecrawl REST works now; the NATIVE web_search tool still needs a Hermes restart to pick up the key. So A's SMA-portal [VERIFY ON SCREEN] step is unblockable once Hermes restarts.
5. STRATEGIC DIVERGENCE worth naming: A/B/C are deepening the OWNER product (Array Operator: monitoring, billing, history, fault alerts for operators like Paul/Bruce). D is a different GTM (sell settlement CERTIFICATION to INVESTOR fleet owners). Same engine substrate, two business models. They're complementary but compete for your focus — the owner product has live paying-path users (Paul), the certification venture has an unproven WTP.

═══════════════════════════════════════════
VERIFICATION / HEALTH (current)
═══════════════════════════════════════════
- Test suite: 996 green. The ONE failure is a pre-existing Chint date-flake — proven by multiple agents to fail identically on clean/old code. Not anyone's damage.
- Prod health 200. Latest backend deploys: A's poller 4217c4a7, D's engine b1a786a.
- All agents stayed within their own files; cron auto-commit swept changes to main as expected; no clobbering reported.

═══════════════════════════════════════════
OPEN THREADS — consolidated & deduped (your call on order)
═══════════════════════════════════════════
INFRA / SCALE
- [A/C] Poller scaling at FULL fleet — A's governor handles per-key budget, but C's note about full-fleet poll >60s on the 5-min tick is the residual; site-level batching is the path. (A's rewrite largely addresses this — confirm whether C's concern is now moot post-A.)
- [A] Consolidate the inverter-alert/warranty detection onto the shared budget-governed scan (3–4×/day). Parked.
- [A/D] SMA going live — blocked on YOU registering an SMA developer app (client_id/secret/system_id). Runbook at solar-operator/docs/SMA_REGISTRATION_RUNBOOK.txt (+ both desktops). D restored web access → can verify the portal's exact button labels after a Hermes restart.

PRODUCT (owner side)
- [B] Seed Paul's 4 real customers w/ their % splits (incl. Danville 95/5) for a full end-to-end he sees on login.
- [B] NEPOOL→Array Operator dashboard branding cleanup (shell still says "NEPOOL Operator" for AO tenants).
- [B] Paul builds the GMP-invoice auto-detection trigger; everything downstream is done and waiting.
- [C] Cosmetic: PlanBillingCard shows HTML-escaped "Subscription &amp; upcoming reports".
- [C] Fluid-sim spike → productionize when wanted.

PRODUCT (investor side / venture)
- [D] Wire the reconciliation engine into a live surface (the certification artifact path), OR keep it inert until the WTP test.
- [D] Get Chad's email → real Encore send (the actual WTP test for the certification pivot).
- [D] Certificates for other Tier-1s (Greenbacker, Sunwealth) for the first outreach wave.

YOUR INBOX (eyeball)
- 2 real verification emails (operator "ready to review" + customer invoice w/ 3 attachments) from B's test.
- 2 draft pitches from D (Encore v1 leak-framing + v2 certification-framing).
