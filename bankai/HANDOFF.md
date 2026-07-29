# BankAI — agent handoff (2026-07-29)

Read this first. It is the state of the project as of the last session, written for
the next agent picking up mid-stream. The user is Ford (ford.genereaux@gmail.com);
the product is a household finance+legal AI copilot for Ford and their husband.

## What this is

`bankai/` is a fully standalone app inside the solar-operator repo (own deps, own
SQLite DB, zero imports from solar-operator code). Read-only by construction — no
code path can move money. Core pieces, ALL BUILT AND VERIFIED except where noted:

- **Ingestion**: SimpleFIN Bridge connector (live sync every 6h + /api/sync),
  CSV import (column auto-detect), OFX/QFX import (FITID dedupe + ledger balance —
  covers BofA/Fidelity/Amex "Quicken" exports and Apple Card Wallet exports).
  All funnel through `ingest.py` (fingerprint dedupe, idempotent re-imports).
- **Model**: accounts / transactions / balance snapshots / categorizer /
  recurring-series detection with next-date prediction (`intelligence/`).
- **Agent**: `agent/tools.py` (tool defs + executors) + `agent/chat.py`
  (`run_turn()` dispatches to pluggable backends in `agent/backends/`):
  `anthropic` (API key), `claude-cli` (headless `claude -p` billing a Claude
  subscription; tools exposed via `agent/mcp_server.py`, a hand-rolled
  newline-delimited JSON-RPC MCP stdio server), `grok` (xAI, OpenAI-style tools).
  Selected by `LLM_BACKEND` env. Default model `claude-opus-5`.
- **Persistent memory**: `MemoryNote` model + `save_memory`/`delete_memory` tools;
  notes injected into system prompt every turn (8k char budget) via
  `chat.build_system(session, channel)`. Dashboard has a memory panel.
- **One persistent thread**: ALL chat (web + SMS) is stored in `ChatMessage` and
  shares one history (`messaging/thread.py: build_history/handle_web/handle_inbound`),
  speaker-labeled (`[Ford] ...`). No in-memory history anywhere.
- **SMS (Twilio)**: mirrored 3-way group thread (Twilio can't join native group MMS):
  inbound relayed to the other spouse, replies broadcast to both. Signed webhook
  `/api/sms/webhook` (HMAC-SHA1, X-Forwarded-Proto aware, `SMS_PUBLIC_URL` override).
  Welcome-on-first-contact + HELP/STOP keyword handling. API-key auth supported for
  sending (`TWILIO_API_KEY_SID/SECRET`); Auth Token still required (signs webhooks).
  Compliance pages served publicly: `/optin`, `/privacy`, `/terms` (for A2P 10DLC or
  toll-free verification — user was advised toll-free verification is the easier path).
- **Rules engine**: reminder / balance_below / large_transaction / bill_reminder /
  weekly_digest, dedupe-key firings (never double-sends), delivery via Resend or SMTP
  email + SMS broadcast. Background loops in `scheduler.py` (sync + rules).
- **Portal**: `static/index.html` — login (shared APP_PASSWORD, HMAC cookie),
  net worth, accounts, month summary, upcoming bills, transactions, statement import
  (CSV/OFX/QFX), rules manager, persistent chat with speaker picker, memory panel.
- **Ops**: `start.sh` (venv+deps+.env bootstrap; `--demo` seeds),
  `scripts/seed_demo.py` (4 accounts / ~170 txns, deterministic).

## Verified end-to-end (in the CCR cloud container)

Live server + real `claude-cli` turns (the container's `claude` CLI is authenticated):
tool-grounded answers from seeded data; memory save + cross-turn recall by the other
speaker ("how are we doing against the grocery budget?" → recalled $500 budget +
"travel card" nickname + live July numbers). 49/49 tests passing at last full run
(`pytest tests` from `bankai/`; repo root pytest.ini only runs solar-operator's own
`tests/`, so no interference).

## Document vault — DONE (2026-07-29, verified live)

User wants the copilot to be a "calm but intense collector" of ALL household records
(home purchase, contracts, legal docs), able to hold files, reread them on demand,
and act protectively with their long-term wealth + best life in mind ("essentially
try to become our lawyer"). Agreed guardrail: max analysis but it must know it's not
a licensed attorney and say when a real professional should review something.

All shipped and verified (64/64 tests; live server smoke: upload → dedupe → list →
original saved to `documents/` → dashboard renders it, all confirmed end-to-end):
- `models.py` `Document` + `vault.py` (PDF/docx/text extraction, sha256 dedupe,
  disk originals, snippet search).
- `agent/tools.py`: four vault tools DEFINED **and DISPATCHED** — `list_documents`,
  `read_document` (paged, `READ_PAGE_CHARS`=30k, `next_start_char` continuation,
  thin-text warning for scans), `search_documents`, `annotate_document`. They flow
  through all three backends + the MCP server automatically (all consume `TOOLS`).
- `app.py`: `GET/POST /api/documents` (15MB cap, returns `created` false on dupe),
  `DELETE /api/documents/{id}`. GET also returns `categories` for the UI.
- `static/index.html`: Document vault section (upload form + table + remove with
  confirm; agent annotations surface under each title). Also fixed a pre-existing
  gap: `doLogin()` now loads thread/memories/docs (before, fresh logins showed an
  empty chat until reload).
- Persona rewrite in `chat.py SYSTEM_PROMPT`: calm-but-intense collector, protective
  long-term stance, one-ask-at-a-time record requests, standing "Household picture" +
  "Document intake checklist" memory notes, reread-the-source rule, and the
  not-a-licensed-professional guardrail — locked by `test_persona_keeps_the_guardrails`.
- `requirements.txt` +`pypdf>=4.0`; `.gitignore` covers `bankai/documents/`.
- Tests: `tests/test_vault.py` (extraction incl. generated docx + pypdf blank-page,
  add/dedupe/delete/search, all four tool dispatches incl. paging) + persona test.

## Real-estate comps tracker — DONE (2026-07-29, live on Ford's real home)

Ford: track neighborhood comps so the AI can actively adjust home value. Built and
live (84/84 tests):
- `models.py`: `Property` (1:1 with a manual property Account — the account balance
  IS the value), `Comp` (source: rentcast|manual|agent), `Valuation` (every value
  with method avm|comps_median|manual|agent + evidence, `applied` flag).
- `realestate.py`: recency×distance-weighted $/sqft median estimate (falls back to
  raw prices without sqft; comps >18mo excluded), RentCast AVM fetch
  (`RENTCAST_API_KEY`, free tier 50/mo — NOT yet configured, Ford must sign up),
  `refresh_property` (fetch → upsert comps → estimate → Valuation → auto-apply w/
  snapshot when `auto_update`).
- Agent tools: `get_property_valuation`, `add_property_comp`,
  `set_property_value` (the agent's ONLY balance write; property accounts only,
  reasoning recorded as a Valuation). Persona has a Real estate paragraph.
- API: GET/POST `/api/properties`, POST `/api/properties/{id}/refresh`,
  POST `/api/properties/{id}/comps`, DELETE `/api/comps/{id}`. UI: Real estate
  section under Net worth (comps table, add-comp form, market-refresh button that
  disables without the key, auto-apply toggle, attach-tracking form).
- Scheduler: `_realestate_loop` refreshes all properties every
  `REALESTATE_REFRESH_DAYS` (default 7) — only when the RentCast key is set.
- LIVE STATE: Ford's home (36001 Cabrillo Dr, Fremont CA 94536, 3bd/2ba 1,148sqft,
  bought Feb 13 2026 for $1.46M) is tracked with 7 REAL sold comps hand-gathered
  from its Redfin page; value auto-applied at $1,398,000 ($1,218/sqft weighted
  median). Known divergence, recorded in the Household picture note: Redfin AVM
  said $1,649,510 on 2026-07-29; we deliberately use sold comps, not AVMs.
  NEXT: Ford signs up at rentcast.io (free) → `RENTCAST_API_KEY` in .env → weekly
  autonomous refresh replaces hand-gathered comps.

## Super-helper upgrade — DONE (2026-07-29, verified live; email awaits app password)

Ford's ask: "all the bells and whistles for long term financial brilliance… voracious
consumption of personal data… access to my email… it should check in ('still using
your PlayStation subscription?') and try to cancel it." Built (100/100 tests):
- `intelligence/forecast.py`: `cash_flow_forecast` (replays every recurring series
  on its cadence over the liquid balance; lowest-point detection; honest "one-offs
  not included" note) + `spending_anomalies` (category spikes vs trailing 3-month
  baseline, large first-time merchants).
- `connectors/email_harvest.py`: Gmail/IMAP document harvester — X-GM-RAW Gmail
  query syntax, standing financial/legal sweep (DEFAULT_QUERY), attachment
  extraction (pdf/docx/txt/rtf/csv, 15MB cap), sha-dedupe into the vault with
  sender/subject/date provenance; `send_email` (SMTP, same app password) for
  approved outbound. NEEDS: GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env (Google
  2-step verification → App passwords). `EMAIL_HARVEST_DAYS` weekly scheduler loop.
- **Action gate** (the "tries to cancel it" architecture): `AgentAction` model +
  `propose_action`/`list_actions` tools; portal "Copilot actions" panel where a
  HUMAN clicks Approve & run (or Decline); executor kind `email_support` sends
  from the household's own address; every outcome audit-logged. The agent cannot
  execute anything itself — the click is the authorization.
- New agent tools: cash_flow_forecast, spending_anomalies, search_email,
  harvest_email_documents, subscription_audit (annualized costs + likely-sub
  flags), propose_action, list_actions → 18 tools total, all backends + MCP.
- **Web access for the brain**: claude-cli backend now allows WebSearch/WebFetch.
- **Monthly review loop**: on each new month the scheduler posts a real agent
  turn into the shared thread (MONTHLY_REVIEW_PROMPT; marker = MemoryNote "Last
  monthly review"; first tick only initializes so deploys don't surprise-fire).
- Persona: inbox reach ("hunt there FIRST… never curiosity"), initiative rules
  (one check-in at a time, approval gate, usage-can't-be-seen-in-bank-data).
- LIVE VERIFY: real turn ran subscription_audit + cash_flow_forecast +
  spending_anomalies, self-diagnosed the forecast's missing-paycheck blind spot,
  asked the PlayStation check-in verbatim, flagged the two ~$9-10k monthly
  transfers as the real question. DEFERRED HONESTLY: browser-driving cancellation
  (computer use) — needs its own session; the action-gate slot is where it plugs in.

## Where things live

- Working tree: `/home/user/solar-operator/bankai/` on branch
  `claude/joint-banking-ai-dashboard-vp8gyq` (designated branch — develop here).
- PR: https://github.com/Garface111/solar-operator/pull/101 (draft, open, session is
  subscribed to its activity; drive-to-green applies but repo has NO CI).
- A `send_later` self check-in trigger re-arms hourly for the PR
  (trig_015thg7v8BmbK7UfVdPZ8VEe) — re-arm silently if nothing changed; stop when
  PR closes.
- **Standalone repo staged at `/home/user/bankai-standalone`** (fresh git history,
  2 commits, `main`): the user agreed BankAI should be its own repo. The GitHub
  integration CANNOT create repos (403). USER ACTION PENDING: create
  github.com/Garface111/bankai (private). When it exists: `add_repo` it (push
  access), sync latest bankai/ into the staging tree (tar copy, exclude .git/db/env/
  venv/__pycache__), push, close PR #101 with a pointer comment, unsubscribe, and
  continue all work in the new repo.

## User-side state (important)

- User's machine is `root@FordBrain` (Linux). They tried `git clone <repo>` literally
  (placeholder!) — give them real commands. Until the standalone repo exists:
  `git clone -b claude/joint-banking-ai-dashboard-vp8gyq
  https://github.com/Garface111/solar-operator.git && cd solar-operator/bankai &&
  ./start.sh --demo` (may need `apt install python3-venv`; needs their GitHub auth
  for the private repo).
- Twilio: user pasted Account SID + Auth Token AND an API key SID/secret into chat —
  **all should be rotated after setup** (they were told). Nothing stored in the repo.
  No phone number purchased yet; A2P/toll-free verification not done. Compliance
  pages + exact form answers were provided (see /optin, /privacy, /terms).
- LLM backends: user asked for Claude-subscription and "Grok Build credits" support —
  both built. On their box, `LLM_BACKEND=claude-cli` needs Claude Code installed+
  logged in; `grok` needs XAI_API_KEY from console.x.ai.

## Gotchas learned the hard way

- **SQLite cross-process locking**: the claude-cli backend's MCP server is a separate
  process writing the same DB. Fixed via WAL+busy_timeout pragmas (`db.py`) and
  committing the inbound chat message BEFORE the agent turn (`thread.py`). Don't
  reintroduce a long-held write transaction around `run_turn`.
- `pkill -f "uvicorn bankai"` in a Bash tool call kills the shell itself (pattern
  matches the command line) — use `pkill -f "[u]vicorn bankai"`.
- `bankai.db-wal/-shm` sidecars once got committed; amended out + gitignored
  (`bankai/*.db*` patterns in repo-root .gitignore). Watch `git add bankai/` output.
- The repo root `.gitignore` already covers `.env` at any depth.
- `--mcp-config` is passed inline JSON (works on CLI 2.1.220); MCP server needs
  `PYTHONPATH` env (not `cwd`) to find the bankai package, plus `DATABASE_URL`.
- Twilio API keys can't validate inbound webhook signatures — only the Auth Token can.
- Git: commits need the Co-Authored-By + Claude-Session footer (see repo history);
  never push to other branches; after pushing to a NEW branch, open a draft PR.

## Suggested order for the next agent

1. ~~Finish the vault~~ DONE (see above; corrected clone/run commands were also
   already given to the user).
2. When the user creates the `bankai` repo: migrate (steps above), close PR #101.
   NOTE: the `/home/user/bankai-standalone` staging tree lived in the CCR cloud
   container and may be gone — if so, re-stage from `bankai/` (fresh `git init`,
   copy tree excluding .git/db/env/venv/__pycache__/documents).
3. Then the deploy story (Railway config) if the user wants the SMS webhook public.
