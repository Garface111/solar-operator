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

## IN FLIGHT — document vault (user's latest ask, ~40% done, UNFINISHED)

User wants the copilot to be a "calm but intense collector" of ALL household records
(home purchase, contracts, legal docs), able to hold files, reread them on demand,
and act protectively with their long-term wealth + best life in mind ("essentially
try to become our lawyer"). Agreed guardrail: max analysis but it must know it's not
a licensed attorney and say when a real professional should review something.

Done (uncommitted or in this WIP commit):
- `models.py`: `Document` model (title/category/filename/sha256 unique/size/
  content_text/summary/added_at).
- `vault.py`: `extract_text` (PDF via pypdf, .docx via zipfile+regex, plaintext),
  `add_document` (sha256 dedupe, original bytes saved to `documents/` dir),
  `delete_document`, `search_documents` (snippet search). `CATEGORIES` list.
- `agent/tools.py`: imports updated AND the four tool DEFINITIONS were added
  (`list_documents`, `read_document` (paged, 30k chars/call, `start_char`),
  `search_documents`, `annotate_document`).

NOT done:
1. `agent/tools.py` `_dispatch()` handlers for those four tools (defs exist,
   executors DO NOT — calling them now hits `unknown tool`). Implement:
   list → id/title/category/size/added/summary; read → paged slice + total_chars;
   search → `vault.search_documents`; annotate → set `doc.summary`.
2. `requirements.txt`: add `pypdf>=4.0`.
3. `app.py`: endpoints `POST /api/documents` (UploadFile+title+category, 15MB cap),
   `GET /api/documents`, `DELETE /api/documents/{id}` (use `vault.delete_document`).
4. `static/index.html`: Documents section (upload form + table + delete).
5. `.gitignore` (repo root): add `bankai/documents/`.
6. **Persona rewrite** in `chat.py SYSTEM_PROMPT` — the intentions ask. Draft spirit:
   calm, precise, quietly relentless about completing its picture; protective,
   long-term wealth AND quality of life; proactively (gently, one ask at a time)
   requests missing records (deed, mortgage note, insurance policies, wills, titles);
   maintains memory notes incl. a "Document intake checklist" and "Household picture";
   rereads source docs when details matter; not-a-lawyer/advisor flag for
   consequential moves. Keep the existing grounding + read-only + speaker rules.
7. Tests: extraction (txt + generated docx; pdf path can be try/except-skipped),
   vault add/dedupe/search/delete, tool dispatch for the four tools, endpoint-level
   optional. Keep the all-pure-logic/no-network pattern.
8. Run `pytest tests`, then commit + push (see Git rules below).

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

1. Finish the vault (items 1–5 above), rewrite the persona (item 6), tests (7), run
   suite, commit, push to the designated branch.
2. Give the user the corrected clone/run commands for FordBrain.
3. When the user creates the `bankai` repo: migrate (steps above), close PR #101.
4. Then the deploy story (Railway config) if the user wants the SMS webhook public.
