# Scheduled email runbooks + the prod-diagnosis script library (Jun 2026)

Two reusable patterns that recur on this project — emailing Ford a runbook/reminder
on a schedule, and the saved scripts that diagnose capture/ingest/display without
re-deriving them each time.

## A. "Email me tomorrow with what to do and why" — the cron-email-runbook pattern

When Ford asks for a future email (a daylight task he can't do at night, a
reminder, a handoff), build it as a SCRIPT-ONLY cron job, and PROVE the email
works by sending a test copy NOW. Steps that worked end-to-end:

1. Write the email as a standalone Python script under `~/.hermes/scripts/`
   (e.g. `fronius_runbook_email.py`). It reads the Resend key inside the script,
   builds the `Authorization: Bea`+`rer ` header in pieces (the masker mangles the
   literal — see resend-email skill), POSTs via curl, and:
     - on a real `{"id": ...}` → `print(...)` a one-line confirmation (cron
       delivers stdout verbatim to chat),
     - on failure → `raise SystemExit("FAILED: ...")` so the cron error path alerts
       (never silently no-op).
   Email content rule for Ford: lead with WHY (the root cause + what's already
   shipped), then WHAT to do as a short numbered list, then exactly what you need
   back from him (one console line / one screenshot). He thinks in end-user terms
   and wants the single concrete action, not a wall.

2. Schedule it: `cronjob action=create, no_agent=true, repeat=1,
   schedule="<ISO local time>", script="fronius_runbook_email.py"`.
   - `no_agent=true` = script IS the job, stdout delivered verbatim, no LLM tokens.
   - `script` MUST be a BARE FILENAME relative to ~/.hermes/scripts/ — an absolute
     or ~/ path is REJECTED ("Script path must be relative to ~/.hermes/scripts/").
   - The machine clock is the cron clock: `date '+%Z %z'` was PDT/-0700 here, so
     `2026-06-19T08:00:00` = Ford's 8am. Check `date` before picking the time.

3. VERIFY THE PATH BEFORE IT FIRES: run the script once by hand
   (`python3 ~/.hermes/scripts/<name>.py`) so you're not trusting an unproven
   send that only fires once. Then GET `https://api.resend.com/emails/{id}` and
   confirm `last_event: delivered` (not just an accepted id). Tell Ford he'll get
   TWO copies (the test + the scheduled one) and offer to cancel the cron if he
   doesn't want the dup. From a verified domain TO himself can still hit spam —
   tell him to check + mark "not spam".

## B. The prod-diagnosis script library (saved under solar-operator/scripts/)

These were rebuilt from this skill once already; keep them in the repo so the next
"vendor arrays show no data" / "bill looks wrong" report is a 3-script probe, not a
re-derivation. All read-only. Run against PROD via the base64-stdin railway trick
(see capture-vendor reference §11), since `railway ssh` runs the deployed image
without your new local file:
  `B64=$(base64 -w0 scripts/X.py) && railway ssh "cd /app && echo $B64 | base64 -d | python -"`

- `scripts/diag_capture_arrays.py` — per capture-vendor (fronius/sma/chint): count
  Inverter + DailyGeneration + InverterDaily rows and latest day per array. Proves
  the INGEST layer (is the data even in the DB?).
- `scripts/diag_fleet_tree.py` — calls `inverter_fleet.build_fleet_tree(db, tenant)`
  for given tenants, prints per-array daily series + each inverter's
  `current_power_w`. Proves the DISPLAY/API layer (does the tree SERVE the data?).
- `scripts/diag_live_power.py` — per vendor: how many inverters have
  `last_power_w`/`last_power_at` set + fresh(<=24h). This is the one that pinned the
  Fronius asymmetry (chint 16/16, sma 42/42, fronius 132/225 w/ many null/0).
- Sentry probe pattern (org `dyson-swarm-technologies`, project `python-fastapi`):
  put the whole thing in a .py file with urllib + a file-read bearer; never one-line
  the token (masker breaks it). Walk `/issues/{id}/events/?limit=8` timestamps vs
  the fix commit's `git show -s --format=%ci` to prove a crash STOPPED after deploy.

## C. Local card-render QA when the AO canvas falls back to the demo fleet

The signed-in AO canvas shows a 100-array DEMO fleet, so a freshly-seeded single
array often won't appear, and it defaults to GRID view (tiles) — switch to "Tree
view" to get `.sb-col` cards; expand a comb via its `.sb-inv-toggle` button.
Faster + more rigorous for a PURE card-render fix: the changed functions live in an
IIFE (not exported), so EXTRACT them verbatim into a tiny `node` test asserting all
branches — critically the "UNCHANGED for a real numeric 0" guarantee so you prove
API-key/per-inverter vendors are untouched. (Did this for the Fronius "no live feed"
label fix: 4/4, and for the devwork live-power extraction: units+freshness 6/6.)
