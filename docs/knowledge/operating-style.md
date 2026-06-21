# Operating Style on Ford's Live Prod Projects (Solar Operator / EnergyAgent)

How to work on this codebase the way Ford wants. These are durable preferences
observed across sessions — not environment quirks.

## Command discipline — minimize noise, especially read-only "confirm it" probes
Ford runs a command-approval gate and will BLOCK commands he doesn't want — and
he has blocked even harmless **read-only** verification commands (a `curl /health`,
piping mock JSON into a script to watch it no-op). Lesson:
- Don't reflexively fire an extra command just to *confirm* something you already
  have strong evidence for. If a deploy reported SUCCESS, the build logs + a single
  already-approved check are usually enough — don't pile on a second/third "let me
  just verify" curl.
- Prefer ONE decisive verification over a string of confirmatory probes.
- When a command is blocked: STOP the whole workflow immediately, do not retry, do
  not rephrase, do not reach the same outcome another way. Acknowledge and ask how
  he wants to proceed. (The runtime block message says this explicitly; honor it.)
- `curl ... | python3` (pipe-to-interpreter) and `.app`-TLD URLs trigger the security
  scanner's MEDIUM/HIGH warnings → an approval prompt. Avoid piping curl straight into
  an interpreter for routine checks; fetch, then inspect separately, or just trust the
  deploy status.

## Verify real outcomes, but via artifacts you already have
Ford values real verification (he hates blind-guess loops) — but "verify" means
checking the actual artifact, not spamming probes. Good patterns used successfully:
- After an extension build: `unzip -p <zip> <file> | grep <token>` ONCE to confirm
  the fix shipped.
- After a deploy: poll Railway deployment status to SUCCESS (one loop), then move on.
- After a frontend deploy: a single `curl -sI`/status check is fine; don't re-curl
  repeatedly.

## Delegation preference
Ford wants coding delegated to Claude Code (opus) to conserve Hermes tokens and use
powerful models. Pattern that worked this session: write a detailed brief to a file
(`<repo>/<FEATURE>_BRIEF.md`), then `claude -p "read BRIEF.md and implement..."
--model opus --permission-mode acceptEdits --max-turns N --output-format json` as a
background process with notify_on_complete. Then VERIFY the result yourself
(self-reports aren't trusted): `node --check`, a DOM-sim or unit test, git status —
before deploying. Remove the scratch brief file before committing.

## Autonomy boundaries on prod
- `git push` to main auto-deploys to Railway (prod) — treat pushing as deploying.
- For anything unattended/automated that could reach prod, Ford ultimately chose the
  SAFE (human-in-the-loop / PR-gated) path over fully-autonomous auto-merge, even
  after first asking for aggressive. Default to human-gated for prod-affecting
  automation unless he explicitly re-confirms otherwise in the moment.
- Deletion-safety remains absolute (see main SKILL.md): never blind-delete; confirm
  the exact target; prefer the non-destructive path.

## Communication
- He course-corrects mid-task with terse out-of-band messages ("actually do the safe
  one", "stop", "wait") — pivot immediately, the latest message wins.
- When an owner reports "data lost / arrays disappeared / Invalid or inactive
  tenant key": prove the data IS persisted with a backend create→fresh-session
  read test BEFORE touching anything, then look at the frontend masking + session
  auth. See references/session-auth-and-demo-fallback.md. The instinct "their data
  is gone" is almost always wrong — it's a dead session showing demo data.
- On failures he reports terse real-world symptoms and shares screenshots
  (OneDrive/Pictures/Screenshots 1/, newest = "check the screenshot") / console
  output when asked. After ~2 failed speculative fixes, switch to instrumenting from
  real evidence rather than shipping another guess.
