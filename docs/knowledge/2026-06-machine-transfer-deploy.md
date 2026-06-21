# June 2026 machine-transfer session — deploy & recovery log

Concrete session detail backing the SKILL.md sections; kept here so the
umbrella stays lean.

## Pre-crash WIP recovery (worked)
- User report: "you crashed while making the extension capture WEC data
  immediately on login, not on the bill-history tab".
- Found on `origin/feat/v2-rec-fuels`: two snapshot commits the morning of the
  crash — `e846ffb backup: auto snapshot 2026-06-12 08:52` and
  `9eedd72 backup: snapshot before machine transfer (smarthub adapter WIP)`.
- `git diff e846ffb..9eedd72` isolated exactly the in-flight work:
  `extension/smarthub_content.js` (+137 immediate-capture), `api/app.py` (+38),
  `api/adapters/smarthub.py` (+49), manifest 1.5.3→1.6.0.
- Commit `3e3f7c2` on the same branch carried the live-verification notes
  (WEC layout B, Rick Evans acct 982501) in its commit message — commit
  messages on WIP branches are primary documentation.

## Critical prod bug found while diffing
`api/app.py /v1/sync` account map hardcoded `UtilityAccount.provider == "vec"`
— every WEC/Stowe/other-SmartHub bill silently dropped (sync returns 200,
bills never appear). Fix was already on the WIP branch; this made deploying
the branch URGENT rather than optional.

## Cherry-pick vs whole-branch decision
Branch was ~13k lines (WEC fix + v1.6.0 extension + national provider catalog
+ V2 fuel_type/cert_registry columns). Cherry-picking the WEC fix alone was
not viable because `SMARTHUB_UTILITIES` had been rewritten to derive from the
catalog CSVs added on the same branch. Stated tradeoff to Ford in 3 lines,
then merged whole branch. Mitigations that made it safe: additive idempotent
migration (server_default 'solar'), 698-test suite green, Railway health
check + instant rollback.

## Test alignments needed after merge (intended-behavior drift)
- `tests/test_vec_autopop.py` (x2) and `tests/test_vec_adapter.py` asserted
  array names == customerName ("North"/"South"/"WGRBS LLC"). v1.6.0
  deliberately switched array nickname to service address. Aligned tests to
  new contract with explanatory comments.
- `tests/test_demo_seed.py` asserted 4-6 demo clients; seed deliberately grown
  to ~15 (sizing comment at scripts/seed_demo_tenant.py:88). Bounds → 12-20.

## Deploy timeline (verified)
push 12:53 → QUEUED → BUILDING (~1min) → DEPLOYING → SUCCESS at ~12:56.
Manual `railway ssh "python -m api.migrate"` after SUCCESS; verified
fuel_type + cert_registry via information_schema query.

## Loose ends handed to Ford
- v1.6.0 zip at C:\Users\fordg\Desktop\Solar Operator\Archives - Extension
  Builds\ — customer must sideload (store lags).
- VEC immediate-capture path unverified live since the 26.x change.
- Recurring `POST /v1/extension/heartbeat 403` in prod logs — pre-existing,
  stale/unpaired extension key somewhere.
