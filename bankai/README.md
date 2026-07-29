# BankAI — household finance copilot

A self-hosted, **read-only** AI system for Ford + spouse's joint finances. It pulls
banking data into a local model (accounts, transactions, balances), keeps it fresh on a
schedule, and gives you:

- **Chat**: ask Claude anything about your money ("what did we spend on groceries in
  June?", "when is the mortgage due?", "how's net worth trending?"). The agent has
  tools over the local database only.
- **Reminders & auto-messages**: a rules engine that emails you — low-balance alerts,
  large-transaction alerts, upcoming-bill reminders (auto-detected from recurring
  transactions), scheduled reminders, and weekly digests. The chat agent can create
  rules for you ("remind us every 25th to move money to savings").
- **Dashboard**: accounts, net worth, recent transactions, rules, CSV import.

## Read-only, by construction

- **No money movement is possible.** There is no code path that initiates transfers or
  payments — the app only ingests and analyzes.
- **Your bank credentials never touch this app.** The recommended connector is
  [SimpleFIN Bridge](https://beta-bridge.simplefin.org) (~$1.50/mo), which is read-only
  by protocol design: you link banks on their side, and this app holds only a
  read-only access URL. CSV/statement import needs no credentials at all.
- The Claude agent's tools are all read-only against the local DB, except
  `create_rule`/`delete_rule` (reminders only).

## Quick start

```bash
cd bankai
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit: APP_PASSWORD, ANTHROPIC_API_KEY at minimum
python run.py                 # http://localhost:8300
```

Log in with the shared household password (`APP_PASSWORD`). Both of you use the same
instance — accounts carry an `owner` label (ford / spouse / joint).

## Getting your banking data in

**Option A — SimpleFIN Bridge (recommended, automatic):**
1. Sign up at beta-bridge.simplefin.org and connect your banks there.
2. Create an app connection → copy the **setup token**.
3. `python -m bankai.connectors.simplefin claim <SETUP_TOKEN>` — prints the access URL.
4. Put it in `.env` as `SIMPLEFIN_ACCESS_URL`. The scheduler then syncs every 6 hours
   (configurable), or hit "Sync now" in the dashboard.

**Option B — CSV import (works today, zero signup):** download CSV statements from your
bank's website and drop them into the dashboard's import form (or POST
`/api/import/csv`). Column names are auto-detected (date / description / amount, or
debit+credit pairs). Re-importing the same file is safe — dedupe is built in.

## Reminders & auto-messages

Email delivery uses, in order of preference: `RESEND_API_KEY`, else `SMTP_*` settings,
else it just logs. Set `NOTIFY_EMAILS` to both of your addresses (comma-separated).

Rule kinds (create in dashboard, via API, or by asking the chat agent):

| kind | what it does | params |
|---|---|---|
| `reminder` | fixed-schedule message | `day_of_month` or `weekday` (0=Mon) |
| `balance_below` | alert when an account balance drops under a threshold | `threshold`, optional `account_id` |
| `large_transaction` | alert on any new transaction over a threshold | `threshold` |
| `bill_reminder` | alert N days before an auto-detected recurring bill | `days_before` |
| `weekly_digest` | weekly cashflow + upcoming-bills summary email | `weekday` |

## Configuration (`.env`)

See `.env.example`. SQLite (`bankai.db`) by default; set `DATABASE_URL` for Postgres
(e.g. on Railway). `ANTHROPIC_API_KEY` powers chat (model: `claude-opus-5`).

## Tests

```bash
pytest bankai/tests
```

Pure-logic tests (dedupe, recurring detection, rules, CSV parsing) — no network, no
API key needed.

## Notes

- This directory is fully standalone (own requirements, own DB) — it can be lifted
  into its own repo untouched.
- Security posture: single shared password over HTTPS is fine for a two-person
  household app, but do put it behind HTTPS if you deploy it (Railway does this for
  you). Don't expose it unauthenticated.
