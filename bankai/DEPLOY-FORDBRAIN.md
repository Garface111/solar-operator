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

**Requirements:** Python 3.11+, `git`, and systemd (this doc assumes systemd; if
FordBrain is not systemd-based, see §5b).

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
./venv/bin/python -m pytest tests -q     # expect: 368 passed
```

**Run the tests.** They are pure logic — no network, no API key — and they are
your only proof the tree arrived intact before you wire in real credentials.
If the count is lower than 368 you have an older copy of the branch; re-pull.

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

⚠️ **`.env` and `bankai.db` are gitignored and must stay that way.** Never commit
either, and never paste their contents into an issue, PR, or commit message.

### The copilot speaks on its own — know this before you enable email

This is not a passive dashboard. Once it is running it acts unprompted:

| Loop | Cadence | Behaviour |
|---|---|---|
| Tending | `TENDING_INTERVAL_HOURS` (6) | Self-directed work. **Silence is the expected outcome.** |
| Sync wake | after any sync that brings new transactions | Reads what arrived; speaks only if it warrants it. `SYNC_WAKE=false` disables. |
| Check-in | `CHECKIN_INTERVAL_DAYS` (3) | **Always speaks** — arriving is the point. Emails both spouses when email is configured, posts to the thread otherwise. `0` disables. |

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

## 4. First run by hand (do this before making it permanent)

```bash
./start.sh
```

Then, from another terminal:

```bash
curl -s localhost:8300/api/health     # -> {"ok":true,"llm_backend":"…",…}
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
- Which LLM backend is live and what it costs him.
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

## 9. Known gap in the model

There is no way to mark a transaction as an **internal transfer**. The monthly
trust redemption is currently counted as income, which inflates every income
figure the copilot reports. If Ford asks why income looks high, that's why. Fixing
it means a transfer flag on the transaction model plus exclusion from the income
side of summaries — a real change, worth doing deliberately rather than in passing.
