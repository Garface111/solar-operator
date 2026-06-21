# Smart array dedup: combining GMP↔vendor duplicate arrays

## The problem
GMP data absorption (api/app.py `/v1/sync`) creates one Array per GMP account; the
vendor connect flow creates one Array per inverter site. The SAME physical array
ends up TWICE — a GMP twin + a vendor (SolarEdge) twin. GMP nickname ("Londonderry
Community Solar" / "1a_Londonderry") rarely EXACTLY matches the vendor name
("Londonderry"), so the old exact-name matcher missed it → duplicates.

## Architecture (Jun'26, LIVE)
- **api/array_merge.py** = the keystone.
  - `merge_arrays(db, src_id, dst_id, tenant_id, reason=)` — LOSSLESS engine.
    Reparents EVERY array-keyed table: utility_accounts, gmp_daily_generation,
    daily_generation (with SOURCE-PRIORITY on (array_id,day) collisions —
    `_SOURCE_RANK`: vendor>extension>csv/manual/gmp_api>bill_prorate; weaker dup
    row deleted, no double-count), inverters (array_id + source_array_id),
    inverter_connection (unique(array_id)→only moves if dst has none),
    warranty_claims, verification_checks, billing_report_subscriptions (+ patches
    array_allocations JSON). Soft-deletes src, writes a DeleteHistory undo row
    (op=array_merge, 30-day token). Does NOT commit (caller commits). The OLD
    account.merge_array_into ONLY moved utility_accounts → orphaned GMP+inverter
    data; it now delegates to this engine.
  - `find_duplicate_pairs(db, tenant_id)` — scores same-tenant pairs:
    STRONG (identical normalized name / same NEPOOL id), MEDIUM (one name
    CONTAINS the other AND cross_source vendor≠gmp), WEAK (same client overlapping
    name → suggest only). `_preferred_destination` makes the array WITH a vendor
    InverterConnection survive (it owns inverters/telemetry).
- **api/jobs/array_dedup.py** — `sweep_tenant`/`sweep_all_tenants(execute=)`.
  DRY-RUN by default. Auto-merges STRONG+MEDIUM, leaves WEAK as suggestions.
- **Absorption-time prevention** (api/app.py `_find_array_to_absorb_into`): GMP
  capture now ATTACHES to an existing vendor twin (exact-name OR cross-source
  containment, guarded) instead of spawning a dup. Wired into BOTH sync create
  branches (autopop + new-client).
- **Scheduler**: `_run_array_dedup` daily 04:45 UTC (execute=True).
- **Admin**: POST /admin/array-dedup/sweep?execute=, /admin/array-dedup/tenant/{id}?execute=
  (503 in prod w/o ADMIN_API_KEY → run via railway ssh python).

## ⚠️ THE SUB-ARRAY GUARD (data-loss landmine — dry-run caught it)
`_differs_only_by_subarray_token(n1,n2)` must return True (→ NEVER merge) for
distinct sub-meters of one site. PITFALL: the first version only fired when names
differed SOLELY by a sub-array token, so a label prefix ("1a_") DEFEATED it and the
prod dry-run would have collapsed `1a_Starlake_North/South/Center` → one `Starlake`,
mixing 3 real arrays' production. FIXED rule: fire whenever a positional token
(north/south/center/roof/lot/carport/1/2/a/b/phase/block…, `_SUBARRAY_TOKENS`)
appears in EXACTLY ONE name AND the two names share ≥1 substantive (non-token) stem
word. Then `1a_Starlake_North` vs `Starlake` correctly = siblings (don't merge),
while `1a_Londonderry` vs `Londonderry` = real twin (merge). `_norm_name` splits on
`_,.-/()` so `1a_starlake_north` → tokens {1a, starlake, north}.

## "Still see duplicates in the sandbox" — the GMP positional-prefix bug
The sandbox (api/sandbox.py GET /v1/sandbox/canvas) renders live clients→arrays→
accounts. After the first dedup pass, Ford still saw dupes like `1a_Tannery Brook`
next to `Tannery Brook`. ROOT CAUSE: GMP's portal prefixes labels with a positional
code `<digit><opt letter>_` ("1a_Chester", "1b_Pittsfield Garage"). There's an
adapter-boundary stripper `api/adapters/_gmp_clean.py clean_gmp_nickname()` applied
in adapters/gmp.py — but arrays seeded/imported via OTHER paths kept the raw prefix.
So `_norm_name("1a_Tannery Brook")` = "1a tannery brook" ≠ "tannery brook" → only
matched as CONTAINMENT, and since both were bare shells (no GMP account → not
cross_source) they fell to WEAK = suggestion only, never auto-merged. FIX: apply the
same `_GMP_POSITIONAL_PREFIX = ^\d{1,3}[a-zA-Z]?[_\-]+(?=\S)` strip inside
array_merge._norm_name. Then prefixed twins normalize identically → STRONG →
auto-merge, while `1a_Starlake_North` → "starlake north" KEEPS the positional token
so the sub-array guard still blocks it. Bonus: app.py `_find_array_to_absorb_into`
imports `_norm_name`, so absorption-time matching is fixed too (no new prefixed
dupes). Verified: demo 22→19 arrays, 0 remaining auto-mergeable dupes, 0 orphaned
daily rows. NOTE: legit singletons that merely carry a prefix (1a_Chester whose only
twin is under a DIFFERENT client; 1b_Pittsfield Garage with no twin) are correctly
NOT merged — they're real arrays with an ugly name, not duplicates.

## Key data constraints (verified on prod)
- `uq_account_per_tenant` = (tenant_id, provider, account_number) UNIQUE → two
  arrays in a tenant can NEVER share a utility account. The "shared UA" signal is
  effectively unreachable; NAME is the real workhorse signal. (shared_ua branch
  kept as defensive dead code.)
- GMP UtilityAccounts have `service_address`=NULL + `extra`={} ; vendor
  InverterConnection.config only has api_key+site_id → NO shared address/key
  signal exists. Name-based matching is the only cross-source option, hence the
  conservative guard.

## Deploy + execution playbook (followed Jun'26)
1. Push (no migration — no new columns). Wait deploy. Verify modules import via
   `railway ssh "python -c import api.array_merge, api.jobs.array_dedup"`.
2. **ALWAYS dry-run first**: `sweep_all_tenants(execute=False)` → inspect every
   AUTO[MEDIUM/STRONG] line BY NAME. This is where the Starlake bug surfaced.
3. Fix anything that looks like a real sub-meter collapse, redeploy, re-dry-run.
4. Get Ford's explicit go before `execute=True` (irreversible-ish; undo-logged).
5. Execute, then VERIFY lossless: src.deleted_at set, src_daily_left==0, dst
   carries the data + its vendor connection, undo tokens written. First run merged
   2 twins (1a_Londonderry→Londonderry, 1b_COVER→Cover Catamount), demo 24→22
   arrays, Trends still renders merged GMP+SolarEdge on one array.
- Shared tree: stage ONLY your files (co-agents have untracked work). Tests:
  tests/test_array_merge.py (DATABASE_URL=sqlite). uq_array_per_tenant blocks
  same-name arrays in one tenant → use distinct names in fixtures.
