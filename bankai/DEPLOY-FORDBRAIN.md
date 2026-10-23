# BankAI — deployment handoff for the FordBrain agent

**Your mission:** get BankAI running *permanently* on this machine (FordBrain) —
surviving reboots, crashes, and terminal sessions closing — and then verify it is
actually up rather than reporting that it should be.

Everything below has been checked against the code, not remembered. Where a step
has a trap in it, the trap is called out inline.

The user is Ford (`ford.genereaux@gmail.com`). The product is a read-only household
finance + legal copilot for Ford and their husband.

---

## 0. Ground truth before you start

- **Nothing is running today.** As of this handoff there is no BankAI process on
  any machine, no `.env` anywhere, and no configured credentials. This is a
  from-scratch deployment, not a repair.
- **The code lives in a subdirectory of another repo.** It is `bankai/` inside
  `github.com/Garface111/solar-operator`, on branch
  `claude/joint-banking-ai-dashboard-vp8gyq` (PR #101). The `bankai/` tree is
  fully standalone — own dependencies, own SQLite DB, zero imports from
  solar-operator code — so you copy that one directory and nothing else.
- **A standalone `bankai` repo does not exist yet.** Ford has to create it
  (agents get 403 creating repos). Do not wait for it. Deploy from the branch
  above; migrating the git remote later is a one-line change and does not
  affect a running install.
- **Read-only by construction.** No code path can move money. Keep it that way:
  if a task would add a tool that writes to a bank, stop and ask Ford first.
  `test_the_agent_has_no_tool_that_writes_source` must keep passing.
- **One exception to "never acts on the outside world":** `cancel_subscription`
  emails merchants directly, with no dashboard Approve. Ford authorized this
  standing power on 2026-08-10. **Read §10 before deploying it** — its
  authorization check is weaker than its docstring says.

**Requirements:** Python 3.11+, `git`, and systemd (this doc assumes systemd; if
FordBrain is not systemd-based, see §5b). For the Saturday printed report you
also need **CUPS** — see §3a; skip it and everything else still runs.

---

## 1. Get the code

```bash
sudo mkdir -p /opt/bankai
sudo chown "$USER:$USER" /opt/bankai
git clone --branch claude/joint-banking-ai-dashboard-vp8gyq \
  https://github.com/Garface111/solar-operator.git /tmp/so
cp -r /tmp/so/bankai/. /opt/bankai/
rm -rf /tmp/so
cd /opt/bankai
```

`/opt/bankai` is a suggestion, not a requirement — but whatever you pick, the DB
and `.env` live *inside it* (`config.BASE_DIR` is the directory containing
`run.py`), so do not move the tree after setup without moving `bankai.db` with it.

---

## 2. Virtualenv + dependencies

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m pytest tests -q     # expect: 550 passed
```

**Run the tests.** They are pure logic — no network, no API key — and they are
your only proof the tree arrived intact before you wire in real credentials.
If the count is lower than 550 you have an older copy of the branch; re-pull.
A `ModuleNotFoundError: fpdf` means the venv predates the `fpdf2` requirement —
re-run the `pip install -r` above rather than hunting for a bug.

Safe to run in the deployed tree: `tests/conftest.py` pins `DATABASE_URL` to a
throwaway file *before* the first bankai import, so the suite can never touch the
live household DB even though `.env` sits right there pointing at it.

---

## 3. Configure `.env`

```bash
cp .env.example .env
```

Then edit. **Minimum viable set — the app will not start usefully without these:**

| Variable | What to put |
|---|---|
| `APP_PASSWORD` | The shared household password. This is the *entire* auth model. Make it long. |
| `LLM_BACKEND` | `anthropic`, `claude-cli`, or `grok` — see below. |

**Choosing the backend** (this is a real cost decision, so surface it to Ford
rather than picking silently):

- `anthropic` + `ANTHROPIC_API_KEY` — pay per token. Most predictable.
- `claude-cli` — bills Ford's existing Claude subscription instead of per-token.
  Requires the `claude` CLI installed on FordBrain **and already logged in as
  Ford**. Verify with `claude -p "say ok"` as the *service user* before relying
  on it; a login that only exists in Ford's interactive shell will not be visible
  to a systemd service running as another user.
- `grok` — billed to Grok Build prepaid credits with no key at all if `grok login`
  or Hermes `xai-oauth` is live on this host (reads `~/.grok/auth.json`, falls
  back to `~/.hermes/auth.json`). Same service-user caveat as above. A classic
  `XAI_API_KEY` also works.

`LLM_BACKEND` accepts a **comma-separated fallback chain**, and the shipped
default is `claude-cli,grok` — first backend that answers wins, so a subscription
turn is tried before spending credits. If you set a single backend instead, you
have removed the fallback; say so when you report.

If `claude-cli` is the *primary* brain, leave `CLAUDE_CLI_TIMEOUT_SECONDS` at its
600 default. A real tool-using turn (MCP server startup + several tool calls + a
verify pass) legitimately runs past the old 120s, and timing out burns the whole
turn rather than degrading.

**Everything else is optional and can be added later without a reinstall** — the
app degrades gracefully when a connector is unconfigured. Add them when Ford
supplies the credentials:

- **Banking data:** `SIMPLEFIN_ACCESS_URL` (SimpleFIN Bridge, ~$1.50/mo,
  read-only). CSV/OFX/QFX import needs no config at all — use the dashboard.
- **Email thread** (so the copilot answers email): needs *all three* of
  `RESEND_API_KEY`, `EMAIL_INBOUND_ADDRESS`, and `HOUSEHOLD_EMAILS`
  (`Ford:ford@…,Partner:…`). `messaging/email_thread.configured()` gates on
  receive + send + household list; miss one and polling silently never starts.
  Only allowlisted senders are ever answered — that is deliberate, do not relax it.
- **Document harvesting:** `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` (app password,
  not the account password). Read-only usage: never sends, deletes, or marks mail.
- **SMS thread:** the `TWILIO_*` block. Note Ford's existing Twilio credentials
  were pasted into a chat and **must be rotated before use** — do not deploy the
  old Auth Token or API key secret.
- **WhatsApp:** `WHATSAPP_ENABLED=true` + `WHATSAPP_DATA_DIR`, plus the Node
  bridge in `whatsapp-bridge/` (its own systemd unit ships there). Two ways to
  run it, and they have different consequences — see below.

**Which account the bridge is paired to is the decision that matters.**

- *Its own number* — the copilot has a WhatsApp account of its own. Cleanest:
  it speaks as itself, and a ban costs nothing but that number.
- *A spouse's number* ("over the shoulder", `WHATSAPP_ACCOUNT_OWNER`) — the
  bridge borrows a human's session. It reads what that person can see, so treat
  it as the more invasive option and make sure both spouses know. Set
  `WHATSAPP_WATCH_ONLY=true` and the copilot **never sends** on that account —
  enforced in `poll_once`, not merely asked of the model; replies still land in
  the dashboard thread. If it *is* allowed to speak, every outbound message is
  stamped with `WHATSAPP_SEND_PREFIX` (default `🤖 `) in `send_group_message`
  itself, so a message on a spouse's account can't be mistaken for the spouse.
  Don't remove that marker — honest attribution is the price of that mode.

**Group pinning is fail-closed.** With `WHATSAPP_GROUP_JID` unset the copilot
watches **no group at all**; it logs each group JID it sees so you can pin the
right one. Set it to the family group and that is the only group it reads or
answers in. Direct messages need no pin — only chats between household members
are ever read.

Sender identity is matched on phone number or privacy LID
(`WHATSAPP_HOUSEHOLD_LIDS`), **never** on push name — a display name is text
anyone can set. Don't "improve" identification by trusting `push_name`.

⚠️ **`.env` and `bankai.db` are gitignored and must stay that way.** Never commit
either, and never paste their contents into an issue, PR, or commit message.

### The copilot speaks on its own — know this before you enable email

This is not a passive dashboard. Once it is running it acts unprompted:

| Loop | Cadence | Behaviour |
|---|---|---|
| Tending | `TENDING_INTERVAL_HOURS` (6) | Self-directed work. **Silence is the expected outcome.** |
| Sync wake | after any sync that brings new transactions | Reads what arrived; speaks only if it warrants it. `SYNC_WAKE=false` disables. |
| Check-in | `CHECKIN_INTERVAL_DAYS` (3) | **Always speaks** — arriving is the point. Emails both spouses when email is configured, posts to the thread otherwise. `0` disables. |
| Weekly report | Saturday from 08:00 (`WEEKLY_REPORT_WEEKDAY` / `_HOUR`) | Renders a one-page PDF and **prints it**, then delivers the numbers by email/thread regardless. `WEEKLY_REPORT=false` disables. |
| Sentinel | `SENTINEL_INTERVAL_MINUTES` (60), first sweep ~30s after boot | Security posture self-audit + ledger integrity + threat watch. Detects and alarms; **never changes controls**. `SENTINEL_ENABLED=false` disables. See §5c. |
| WhatsApp | `WHATSAPP_POLL_SECONDS` (10), only when `WHATSAPP_ENABLED=true` | Listens in the family group, logs money mentioned in passing via `log_expense`, and mostly stays quiet — one turn per batch, not one reply per message. |

So the moment `HOUSEHOLD_EMAILS` + a send path are set, **the copilot starts
emailing the household every three days by itself.** That is the intended design,
but Ford should be told it's about to start rather than discovering it in his
inbox. First tick after a fresh deploy only initialises the marker — it will not
surprise-mail on day one.

Outbound is household-only by construction: `email_thread.start_thread()` resolves
recipients from `HOUSEHOLD_EMAILS` and nothing else, never from an inbound
message's To/Cc. Anything aimed at an outside party must go through
`propose_action` and the dashboard's human approval gate. **Do not add a code path
that emails a non-household address.**

---

### 3a. The printer

Printing is not just the Saturday page — the copilot holds three tools it can
reach for in any conversation: `print_weekly_report` (regenerate and print the
page on demand), `print_page` (anything it writes — a shopping list, a draft
letter), and `print_document` (a paper copy of something already in the vault).
So if Ford ever says "print that," this is the section that decides whether it
works.

Every path shells out to `lp -d "$PRINTER_NAME"`, so it needs a CUPS queue
that exists **for the service user**, not just in Ford's desktop session:

```bash
lpstat -p                     # list queues visible to you
lpstat -d                     # default destination
```

If the queue is missing, register it once (adjust the URI to the real printer)
and name it to match `PRINTER_NAME` — the default is `household`:

```bash
sudo lpadmin -p household -E -v ipp://<printer-ip>/ipp/print -m everywhere
lp -d household /usr/share/cups/data/testprint    # prove it before trusting it
```

Then run `lpstat -p household` **as the systemd service user** (`sudo -u <user>
lpstat -p household`). A queue that only exists for Ford's login will make every
Saturday print fail while the rest of the report still arrives.

`WEEKLY_REPORT=false` stops the *scheduled* Saturday page. It does not remove the
print tools — those stay available whenever the household asks. With no working
queue they simply report the failure and keep the PDF.

A dead or absent printer is *not* fatal by design — the PDF is saved, the numbers
and narrative still go out by email or thread, and the failure is reported
honestly rather than swallowed. Don't "fix" that by making it fatal.

PDFs accumulate in `bankai/reports/` (gitignored). Nothing prunes them; if the
household prints a lot, add them to whatever cleanup you set up for backups.

---

## 4. First run by hand (do this before making it permanent)

```bash
./start.sh
```

Then, from another terminal:

```bash
curl -s localhost:8300/api/health     # -> {"ok":true}   (liveness only)
```

`/api/health` is deliberately bare — it used to leak the LLM backend, model, and
full xAI auth status (account email, team id, other agents' on-disk auth paths)
to anything that could reach the port. The diagnostics moved to
**`/api/health/detail`**, which requires a session cookie. To read it, log in
first and reuse the cookie:

```bash
curl -s -c /tmp/bankai.jar -X POST localhost:8300/api/login \
  -H 'content-type: application/json' -d '{"password":"<APP_PASSWORD>"}'
curl -s -b /tmp/bankai.jar localhost:8300/api/health/detail   # backend, model, xai status
rm -f /tmp/bankai.jar
```

Open `http://localhost:8300` and log in with `APP_PASSWORD`. Confirm the
dashboard renders. Only once this works by hand should you turn it into a service
— debugging a failing unit file is far worse than debugging a failing command.

`./start.sh --demo` seeds realistic demo data first if Ford wants to see it
populated before connecting real accounts. **Do not run `--demo` against a
database that already holds real household data.**

---

## 5. Make it permanent (systemd)

The scheduler — bank sync, rules, email polling, the autonomous tending pass —
runs *inside* the web app's lifespan (`app.py` `lifespan()` calls
`start_background_tasks()`). **One service is all you need.** Do not write a
second unit for a worker; there isn't one.

Create `/etc/systemd/system/bankai.service`:

```ini
[Unit]
Description=BankAI household finance copilot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=REPLACE_WITH_SERVICE_USER
WorkingDirectory=/opt/bankai
ExecStart=/opt/bankai/venv/bin/python -m uvicorn bankai.app:app --host 127.0.0.1 --port 8300
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bankai
systemctl status bankai --no-pager
journalctl -u bankai -f          # watch it boot
```

Three things about that unit file are deliberate:

1. **`--host 127.0.0.1`, not `run.py`.** `run.py` binds `0.0.0.0`, which puts a
   single-shared-password login page in front of every device on the home
   network. For an always-on service that is the wrong default. Binding to
   loopback keeps it to this machine. If Ford wants it reachable from his phone
   on the LAN, that is a conscious decision — put it behind a reverse proxy with
   TLS, or a firewall rule scoped to specific devices, and tell him what he's
   accepting. Don't just flip it to `0.0.0.0`.

   **If you do expose it, it must be HTTPS.** The session cookie is set with
   `Secure`. Browsers treat `http://localhost` as a secure context and accept it
   there, but over plain HTTP to a LAN address (`http://10.0.0.x:8300`) the
   browser **silently drops the cookie** — login returns 200 and every request
   after it 401s, which reads like a broken password rather than a missing
   scheme. Terminate TLS at the proxy and the problem disappears. Do not "fix"
   it by removing `Secure`.

   **For phone access, prefer the tunnel over opening the bind** — see §14.
2. **`Restart=always`.** This is what "permanently" means in practice — it comes
   back from a crash, and `enable` brings it back from a reboot.
3. **No `EnvironmentFile`.** `config.py` loads `.env` itself via `python-dotenv`
   from `BASE_DIR`. `WorkingDirectory` is what makes that resolve; if you drop it,
   the app starts with an empty config and *appears* healthy.

**Verify it is actually permanent — do not skip this:**

```bash
sudo systemctl restart bankai && sleep 3 && curl -s localhost:8300/api/health
sudo reboot          # then, after it comes back:
systemctl is-enabled bankai && systemctl is-active bankai
curl -s localhost:8300/api/health
```

A reboot test is the only real proof. Report the actual output to Ford.

### 5c. Sentinel will grade your deployment

A self-defense subsystem (`bankai/security/`, doctrine in `security/CHARTER.md`)
sweeps hourly and writes to a hash-chained audit ledger. Read `/api/security`
(behind auth) or the dashboard's Defense panel after your first boot — it checks
exactly the things this document tells you to get right, so treat its first
report as your deployment's grade:

- **file permissions** on the tree, DB, and `.env`;
- **loopback-only binding** — it shells out to `ss -tlnH` and flags any listener
  on your port that is not on 127.0.0.1;
- **secrets in logs**, session-secret strength, and whether backups exist.

Two consequences for you:

- If you deliberately expose the app to the LAN behind a TLS proxy (§5), that
  binding check **will alarm**. That is the system working, not a bug. Do not
  silence it by editing Sentinel — tell Ford the alarm is expected and why.
- Sentinel only ever reports. It will not "fix" a finding, and neither should
  the copilot: the charter's whole premise is that the model never holds the keys
  to its own cage. If a future change would let the agent edit security settings,
  the ledger, or its own code, that change is the thing to stop.

`ss` comes from `iproute2`. If it is missing, the binding check degrades to a
`notice` rather than failing the sweep — install it so the check actually runs.

### 5b. If FordBrain is not systemd

Use whatever the platform's supervisor is (launchd on macOS, a Docker container
with `restart: unless-stopped`, or `supervisord`). The requirements are identical:
run `python -m uvicorn bankai.app:app --host 127.0.0.1 --port 8300` with
`/opt/bankai` as the working directory, restart on failure, start on boot.

---

## 6. Back up the database

The SQLite DB at `/opt/bankai/bankai.db` is the household's whole financial
history, the document vault index, and the copilot's persistent memory. It is not
replicated anywhere. Set up a nightly copy before Ford puts real data in:

```bash
# /etc/cron.daily/bankai-backup  (chmod +x)
#!/bin/sh
install -d -m 700 /opt/bankai/backups
/opt/bankai/venv/bin/python - <<'PY'
import sqlite3, datetime, pathlib
dst = pathlib.Path("/opt/bankai/backups") / f"bankai-{datetime.date.today()}.db"
src = sqlite3.connect("/opt/bankai/bankai.db")
out = sqlite3.connect(dst)
src.backup(out)          # online backup — safe while the service is running
out.close(); src.close()
PY
find /opt/bankai/backups -name 'bankai-*.db' -mtime +30 -delete
```

Use `sqlite3`'s backup API as above rather than `cp` — the DB runs in WAL mode
with live writers, and a naive copy can capture a torn state.

The `documents/` vault directory (original uploaded files) is also gitignored and
also unreplicated. Include it in whatever backup Ford already runs for the machine.

---

## 7. Once it's up — report this to Ford

Tell him plainly:

- The URL and that it is loopback-only (and what that means for phone access).
- Which LLM backend is live and what it costs him — read it from
  `/api/health/detail`, not `/api/health`, which now reports liveness only.
- Which connectors are configured vs. still dark. Be specific — "email replies
  are not on yet because `RESEND_API_KEY` is unset" beats "mostly working."
- The reboot-test result, quoted.

**Do not claim a connector works until you have seen it work.** Ford trust-checks
output, and a confident wrong status is worse here than an admitted gap.

---

## 8. Standing items you inherit

These are open and blocked on Ford, not on you. Mention them once; don't nag.

1. **Create the standalone `bankai` repo (private).** Until then the code only
   exists on a branch of solar-operator.
2. **`solar-operator` is a PUBLIC repo**, and `bankai/HANDOFF.md` on that branch
   contains Ford's home address, purchase price, and property valuations. Making
   the repo private, or scrubbing that file, is Ford's call — he has been told.
   **Do not add any further personal data to files in this repo**, including this
   one.
3. **Rotate the Twilio Auth Token and API key secret** before any SMS deployment.

## 9. Internal transfers — closed, but needs one setup step

This used to be a real gap: the monthly trust redemption counted as income and
inflated every income figure. It is fixed. The `transfer` category is now
excluded from both the income and spend sides of `spending_summary()` and of the
weekly report's figures, and the copilot has a `recategorize_transactions` tool
that can relabel matching transactions and persist the correction as a standing
`CategoryRule`, so future syncs label them the same way.

**But nothing is labeled until someone labels it.** On a fresh deploy the trust
redemption still lands as income. Once real data is in, ask the copilot to
recategorize it — something like *"the monthly redemption from the trust is a
transfer between our own accounts, not income — fix it and remember"* — and it
will relabel the matches and write the standing rule.

Two things to watch when it does this:

- The tool refuses an empty selection and requires `description_contains` to be
  at least 4 characters, but a short generic pattern (`card`, `visa`) can still
  sweep in far more than intended, and `remember` defaults to true — so a broad
  pattern becomes a *permanent* rule. Have it report what it changed, and check
  the count is what you expected.
- Category rules outrank the shipped keyword table by design. That is correct —
  the household's correction should beat our guess — but it means a bad rule
  keeps winning until someone overwrites it with the same pattern.

---

## 10. `cancel_subscription` — the standing power, and the gap in its guard

Ford authorized this on 2026-08-10: the copilot may cancel a subscription by
emailing the merchant, with **no dashboard Approve**. It is the only tool that
acts on the outside world unattended. Flagging it here because the deploying
agent should understand what is and is not actually enforced before it runs
against real accounts.

**What genuinely holds, in code:**

- **Guarded categories are refused.** `guard_reason()` runs *before* execute and
  blocks insurance, health, utilities, phone/internet, mortgage/rent, and debt —
  by transaction category *and* by merchant-name keyword, so a missing category
  doesn't open the door. Those become `propose_action` proposals instead.
- **Template-only outbound.** The model picks the merchant and the recipient
  address; it does **not** write the prose. So a hostile transaction description
  or document cannot turn this into a general exfiltration channel.
- **Audited and self-verifying.** Every execution writes an `AgentAction`
  (status `executed`) and plants an `on_date` watchpoint ~35 days out to check
  the merchant actually stopped charging, escalating if not.

**The gap — read this part carefully.** The module's docstring says the
"only on a spouse's instruction" rule is "enforced HERE, in code, not in the
prompt." It is not. `instructed_by` is a **string the model supplies**, and the
check is only `instructor in household_members()` — it validates that the *name*
is Ford or Gaurav, not that either of them said anything. Nothing ties the
execution to an actual household message.

That matters because this copilot ingests untrusted content by design — emailed
attachments, harvested documents, WhatsApp messages, transaction descriptions —
and the Sentinel charter's own premise is that a prompt is a disposition, not a
wall. A successful injection that gets the model to call `cancel_subscription`
with `instructed_by="Ford"` sends a real cancellation to a third party with no
human in the loop. The guarded list bounds the damage to a non-essential
subscription, and it is recoverable, but it is still an outside-world action on
the model's own say-so.

**Do not "fix" this by editing the docstring to match the code.** The honest
fixes are either:

1. bind authorization to evidence — require a recent household `ChatMessage`
   that requested it, and store that message's id on the `AgentAction`; or
2. route cancellations through `propose_action`'s Approve gate like everything
   else, and accept that it is no longer a standing power.

Which one is Ford's call, not yours. Until he decides, deploy with the power
available if he wants it, and tell him plainly that the guard is name-shaped
rather than instruction-shaped.

---

## 11. Self-improvement — the copilot reads its own code

The copilot can now read its own source and write patch **proposals**. The
self-modification lock still holds and you should confirm it yourself:
`test_the_agent_has_no_tool_that_writes_source` passes, and there is no tool that
writes source to disk or deploys anything. The loop is read → propose → evaluate
in a jail → **a human merges**. Verified in code, not taken on faith:

- `read_source` / `list_source` are path-guarded to `bankai/` and `tests/` only,
  with `.env`, `*.db` and `*.sqlite` refused outright.
- `propose_patch` stores full file contents as a `CodeProposal` row and diffs
  them with `difflib` against the deployed tree. No git, no execution, no write.
- Agent-authored tests are treated as hostile code, because they are: they run
  only inside a sandbox that cuts the network (`unshare --net`) and drops to an
  unprivileged user (`setpriv --reuid`). **If no sandbox is configured,
  evaluation is refused rather than faked.**

**If Ford wants evaluation enabled**, run `scripts/setup-selfimprove-sandbox.sh`
as root and set the `SELFIMPROVE_*` block in `.env`. Then **prove the jail before
trusting it** — `selfimprove_sandbox.verify_sandbox()` actively probes it, and
both probes must fail closed: a network connect must not succeed, and reading
`/opt/bankai/.env` from inside must not succeed. Do not report evaluation as
working on the strength of the script having run.

Leaving `SELFIMPROVE_EVAL_ENABLED=false` (or simply not configuring a sandbox) is
a perfectly good deployment. Reading and proposing still work; only the automatic
test-run is off.

**One soft edge worth knowing.** `safe_repo_path()` validates the path *string* —
it rejects absolute paths, `..`, `.env` and database files — but `read_source`
does not resolve the final path to re-check containment. A symlink placed inside
`bankai/` pointing at something outside it would pass the guard and be read. The
copilot cannot create one (it has no write tool), so this is not exploitable by
the model today. Keep it that way: **do not put symlinks in the deployed tree**,
and if you ever add a tool that writes files, resolve the path and verify it is
inside the source root before this becomes real.

---

## 12. Model routing changed when the adversarial verifier runs

Ford asked for a speed fix, and the router (`bankai/router.py`) delivers it by
matching model and effort to the ask: a balance readback runs quick and shallow,
a planning question runs Fable at max effort. Background/tending turns always
take the deep path, and callers that *know* the work is heavy (the Saturday
report, the monthly review) force the top tier via `for_tier()`.

**The part worth understanding before you trust the output.** The adversarial
verify pass — the second model that attacks a reply for hallucinated or
inconsistent numbers — is now gated on the router's decision:

```python
if channel != "sms" and decision.verify:
    reply, report = verify.verified_turn(...)
```

Previously `verified_turn` was always called off-SMS, and *it* decided whether to
critique based on the **answer's** content (dollar amounts ≥ $100, percentages,
recommendations, deadlines). Now the decision is made from the **question's**
shape, by keyword and length heuristics, before any answer exists. A question the
router reads as simple never reaches the content trigger at all.

So "what's our net worth?" — short, no complexity keyword — routes fast with
`verify=False`, and the dollar figure in the reply goes out unchecked. That is
defensible for a pure lookup, where the number comes straight from a DB tool
call; it is thinner ground for something like "how are we doing?", which is also
short and keyword-free but invites a synthesized answer.

This is a deliberate speed/assurance trade, not a bug — but it is Ford's to
calibrate, not yours. If he wants the old assurance back, the smallest honest fix
is to call `verified_turn` whenever the tier is complex **or** the reply contains
money/percentages/recommendations, so the content trigger keeps its say. Setting
`ROUTER_ENABLED=false` restores the previous behavior wholesale at the cost of
the speed win.

---

## 13. Approved changes now build themselves — what actually gates that

Ford's decision (2026-08-11): approving a `code_change` on the dashboard no
longer just files the idea, it dispatches a headless agent that implements it,
runs the tests, and deploys. `BUILDER_ENABLED=false` turns this off and the
approve click goes back to filing the proposal.

**The authorization is the human click**, behind `APP_PASSWORD`. The copilot can
propose its own changes freely; it cannot approve them, and no agent tool reaches
the builder (`test_the_agent_has_no_tool_that_writes_source` still passes).

Three gates stand between an approved idea and production, and all three are
enforced in `builder.py` rather than trusted from the building agent's own
report — which is the right design, since an agent's "tests pass" is a claim, not
evidence:

1. **Scope.** `changed_files()` collects modified, staged **and untracked**
   paths, and `out_of_scope()` refuses anything outside `bankai/` and `tests/`.
   Including untracked files matters — a brand-new file outside the surface is
   exactly what a scope check exists to catch.
2. **Tests.** The suite is re-run in this module's own subprocess, with a
   scrubbed environment (`_ENV_KEEP` passes only `PATH`, `HOME`, locale, `TERM`,
   `USER`, `SHELL`). That scrub is a fix for a real defect found the same day:
   the build gate had been running tests against the household's **live
   credentials**.
3. **Deploy.** Only after 1 and 2, and in a transient systemd scope so that
   restarting `bankai.service` cannot kill the process doing the restarting.

Builds run in a **separate detached worktree on ext4**, never `/opt/bankai`, so
the builder never edits code that is running, never entangles a human's
in-progress edits, and never sees the live DB, vault, or `.env`. One build at a
time, under a lock.

**The property to keep in view.** `builder.py` is itself inside the buildable
surface, so a build could rewrite these guards — but only for the *next* run,
because the guards enforcing any given build are the ones already deployed when
it started. The protection is real and it is exactly one generation deep. That
makes the human reading the reported diff the actual last line of defense, not a
formality. If you are the agent operating this: **read what each build changed
before approving the next one**, and if a build's diff touches `builder.py`,
`selfimprove.py`, or `security/`, treat that as the moment to stop and get Ford's
eyes on it.

---

## 14. Phone access via Cloudflare tunnel — and what it changes

`bankai/tunnel.py` supports reaching the portal from a phone through a
Cloudflare tunnel instead of by opening the bind. That is the right shape:
Cloudflare terminates TLS, **nothing new listens on the network**, and the
origin stays on `127.0.0.1`. Two consequences follow that you should know before
switching it on.

**Sentinel will not alarm on this.** Its binding check looks for a listener on
a non-loopback address (§5c); a tunnel creates none. So unlike a `0.0.0.0` bind,
this exposure is invisible to the posture audit — which is fine, but it means
the audit is not the thing telling you whether the portal is public. Only your
own knowledge of whether `cloudflared` is running does that.

**The password becomes the whole perimeter.** The module's own docstring says
this plainly and it is worth repeating: the tunnel URL is unguessable but it is
not a secret, so `APP_PASSWORD` is what stands between the internet and the
household's entire financial life. On loopback a mediocre password was survivable.
Here it is not. Before enabling a tunnel:

- make `APP_PASSWORD` long and random — this is the one credential that matters;
- confirm the login throttle and the `HttpOnly`/`Secure`/`SameSite` cookie are
  intact (both landed in the hardening pass, §4/§5) — they are load-bearing now
  in a way they were not before;
- tell Ford, in plain words, that the portal is reachable from the internet.
  That is his call to make knowingly, not a detail to bury in a status line.

**The free "quick tunnel" hostname changes on every restart**, which is why the
module watches for the change and emails the household the new link — a phone
bookmark would otherwise rot silently. `announce()` sends through
`email_thread.start_thread()`, so it goes to `HOUSEHOLD_EMAILS` and nowhere else,
same as every other outbound path. A stable hostname needs
`cloudflared tunnel login` in a browser, which no agent can do; if Ford wants
bookmarks that last, that is the step to ask him for.

Nothing starts a tunnel automatically — the app neither launches nor supervises
`cloudflared`. If you set one up, it is a separate unit and a deliberate act.
