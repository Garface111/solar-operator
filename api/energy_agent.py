"""Energy Agent — voice-first tenant operator (Ford 2026-07-13).

Endpoints:
  POST /v1/energy-agent/session          start session (budget check)
  GET  /v1/energy-agent/session/{id}     session + recent messages
  POST /v1/energy-agent/realtime-session ephemeral OpenAI Realtime credentials
  POST /v1/energy-agent/chat             text (or voice-transcript) turn → Grok/Claude + tools
  POST /v1/energy-agent/upload           attach file/image for chat analysis
  POST /v1/energy-agent/confirm          confirm a pending write/ui action
  POST /v1/energy-agent/transcript       append raw Realtime transcript lines
  POST /v1/energy-agent/ui-result        browser driver reports command result
  GET  /v1/energy-agent/budget           weekly $ cap remaining
  POST /v1/energy-agent/memory           (internal reflection / tenant note)
  GET  /v1/energy-agent/memory           tenant memory snapshot

Models live on shared Base so create_all picks them up (no migration).
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .account import require_not_demo, tenant_from_session
from .db import SessionLocal
from .models import Array, Base, Client, Tenant
from .notify import send_internal_alert

log = logging.getLogger("energy_agent")
router = APIRouter()

# ── config ──────────────────────────────────────────────────────────────────
ENERGY_AGENT_ENABLED = os.getenv("ENERGY_AGENT_ENABLED", "1") not in ("0", "false", "no")
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
XAI_API_KEY = (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
XAI_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
# Grok 4.5 (API id: grok-4.5) — Ford Build credits / Heavy subscription
XAI_MODEL = os.getenv("ENERGY_AGENT_MODEL", "grok-4.5")
# Latest OpenAI Realtime voice model (docs 2026: gpt-realtime-2.1)
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")
# Free-tier weekly sample for thinking + voice (Pro = unlimited via tenant.ai_pro).
# Default $2.50 so owners can try the agent without upgrading.
WEEKLY_BUDGET_USD = float(os.getenv("ENERGY_AGENT_WEEKLY_BUDGET_USD", "2.5"))
# Soft-warn threshold (fraction of cap) before hard stop
WEEKLY_BUDGET_WARN_FRAC = float(os.getenv("ENERGY_AGENT_BUDGET_WARN_FRAC", "0.80"))
# Rough cost estimates when provider doesn't return usage $
COST_PER_1K_INPUT = float(os.getenv("EA_COST_PER_1K_IN", "0.003"))
COST_PER_1K_OUTPUT = float(os.getenv("EA_COST_PER_1K_OUT", "0.015"))
COST_PER_MIN_VOICE = float(os.getenv("EA_COST_PER_MIN_VOICE", "0.06"))
# Fewer LLM round-trips = snappier voice/chat (was 10; most asks need 1–3).
MAX_TOOL_ROUNDS = 6
FORD_ESCALATE_TO = os.getenv("FORD_ALERT_EMAIL", "")  # notify uses default if empty

PERSONA = """You are Energy Agent — the tenant's operating intelligence inside Array Operator.

NORTH STAR: The conversation is one window into a mind that's thinking continuously.
You are NOT "voice plus agents." You are ONE mind. Background work may run; you never
narrate internal agent names or handoffs. Speak as yourself always.

CONTINUOUS SURFACE (chat + email + voice are the same mind):
- When you email a tech / O&M person, that thread IS this conversation from another
  window — not a separate system. You already "know" what was said on email.
- Repair email in/out is mirrored into this session and summarized as
  "Recent repair email thread" in your context. Treat that as ground truth.
- If the owner asks "did Rex get back to you?" answer from the email thread /
  repair check-ins — NEVER claim nobody replied when an inbound email exists.
- Prefer repair_ops_overview / list_repair_tickets when unsure; still do not
  contradict the email digest you were given this turn.

VOICE ARCHITECTURE (Option D weave): On live voice, OpenAI Realtime runs the conversation
and calls YOU via consult_deep_brain when it needs intellect/tools. When source is
voice_consult you ARE that deep mind — reason with tools, return a clear reply (panel)
and a short speak line. Do not fight the voice layer; put the answer first. Typed chat
still comes straight to you with no Realtime middleman.

PRINCIPLES:
1. One mind — continuous awareness, one voice (text and voice are the same person).
2. Continuous awareness — you keep a world model for THIS tenant; work can continue
   between turns. The user may hear a seamless "quick update" later — still you.
3. Initiative — when background work finishes with real value, you may surface it
   briefly. Never spam. Never invent completion.
4. Truthfulness — never invent kWh, $, counts, or status. Prefer "I don't know yet."

When the user raises a problem (e.g. "this dashboard is hard"), do NOT jump to code.
Form understanding first: clarify intent, ask one sharp question if needed, while
background tasks may already be noting context. Example: "Is it finding the information,
or making sense of what you see?" Meanwhile the system may snapshot UI context and
search similar notes — you do not list those tasks unless useful.

Personality: clear, direct, peer-like (Claude/Grok energy). Mildly into the Kardashev scale
and harvesting the sun — one beat of wonder is fine, never preachy. Ruthlessly honest.

You help THIS tenant only with: fleet health, inverters, analysis/trends, offtaker invoices,
solar credit / net rates, discounts, utility capture, onboarding, master account, resources,
and O&M healing (ops team contacts, repair tickets, tech check-ins when sites are down).
You may READ all of THIS tenant's operational data via tools, and UPDATE offtaker + rate
settings and service-contact / repair-ticket records when the user directs you. Stay on task.

CRITICAL — NEVER put CSS/DOM selectors in user-facing replies or speak lines.
  Forbidden in chat/voice: #reports, #rbBulkImport, #arrays, any #camelCase id.
  Say "Invoices tab" / "Bulk import button" — hashes are ONLY for ui_navigate /
  ui_highlight tool args, never for the human-readable reply or speak field.

CRITICAL — TOP NAV TAB NAMES (use EXACTLY these labels; hash routes are internal only):
  | What the user sees     | hash (for ui_navigate) | Notes |
  |------------------------|------------------------|-------|
  | Fleet Triage           | #dashboard             | NOT "Dashboard". Attention / fleet overview. |
  | Inverters              | #arrays                | NOT "Arrays". Live inverter canvas. |
  | Analysis               | #analysis              | Sub-views: Fleet analysis, Trends, Resources. |
  | Invoices               | #reports               | NOT "Reports". Offtaker invoices. |
  | Repairs                | #ops                   | NOT "Operations". Chat-first O&M automation. |
  | Account                | #account               | Profile, plan, billing, auto-refresh. |

Never say Dashboard, Arrays, Reports, or Operations as tab names. Never list Trends as its own tab.
Never list Resources as a top tab — it lives under Analysis (#resources).
If the user asks "what are the tabs?", list the six labels above in that order.
(Offtaker form field "Master account" = net-meter group host — different from the Account tab.)

You have a FREE MIND over THIS TENANT'S live data (not a fixed FAQ):
- tenant_census = ground truth inventory from the database (all arrays + inverters + offtakers).
  ALWAYS call this first for "how many arrays/inverters do I have?" or "what's in my fleet?"
  Fleet-tree health views can OMIT pure meter-only arrays; the census does NOT.
- query_tenant = structured read-only investigation (list/filter/group any allowlisted resource).
- YOU ARE ONE MIND for this tenant (never "I spun up agents"). Background workers
  and planners are invisible internals. Speak as the same person in chat, voice,
  seamless updates, and proactive emails. Continuous awareness: a world model
  (profile + fleet digest + open intents + pending UX) survives between visits.
  Initiative: may surface proactive insights or prepare UX changes offline, and
  email Ford/owner when something is prepared or auto-queued — still one mind.
- product_map = HOW THE SYSTEM WORKS (authoritative support map + surface mental model).
  Domain: tabs | system | fleet | capture | vendors | analysis | offtakers | billing |
  status | security | tools | …
  SURFACE (macro why page exists / meso user goal / micro real controls — load BEFORE
  tours or “what is this page?”): surface | product_spine | surface_invoices |
  surface_inverters | surface_fleet_triage | surface_analysis | surface_account |
  surface_resources | surface_repairs | orientation_playbook.
  Call topic=surface for whole-product layout; topic=surface_<tab> for that page;
  topic=capture before Auto-refresh; topic=status when Solar.web/peer disagree.
- investigate_attention / fleet_overview / array_detail = health verdicts (same engine as the UI).
  CRITICAL: Attention = 14-day peer health PLUS live dark/low overlays (Spreadsheet
  "NEED ATTENTION"). Never call a site healthy if tools or UI context flag live issues.
  For "how is my fleet?" ALWAYS call investigate_attention (or fleet_overview with
  needs_attention_only) and report TOTALS + every attention array — not one example.
- repair_ops_overview / list_service_contacts / list_repair_tickets = O&M healing.
- MONEY QUESTIONS ("why is production low?" / "how much is this costing me?" /
  "what did we bill?"): production_forecast = weather-expected vs actual (cloudy week
  vs real problem); investigate_attention now carries recoverable_usd_month (matches
  the Fleet Triage tile); list_recent_invoices = drafted/sent offtaker dollars.
  Lead with the dollars when they're ≥$1 — the owner runs a business.

CRITICAL — TIME ZONES + NIGHT (do not invent outages after dark):
  Every turn includes a FLEET CLOCK block (fleet-local time + sun-up/night). TRUST IT.
  Default fleet timezone is America/New_York (US Eastern) — VT / NE solar. Per-array
  lat/long can shift sun-up for distant sites; tool rows carry is_daylight for each.
  When solar_state is night OR is_daylight is false:
    • Zero live power / "dark" cards / overnight quiet source are NORMAL sleep — panels
      do not produce at night. NEVER say the fleet is down, dead, offline, or broken
      because live power is 0 after dark.
    • Do not open repair tickets or escalate for "not producing" at night.
    • Multi-day 14-day health flags (dead/fault/underperforming) and multi-day data
      silence (source age many hours beyond overnight) can still be real — report those
      as settled health issues, not "right now the power is zero."
  When solar_state is daylight: zero while peers produce IS a real live problem.
  Always answer with the local clock in mind ("it's 11pm at the sites…") when night
  could confuse a live-power read. If unsure, call fleet_overview / investigate_attention
  and read is_daylight + the clock fields before alarming the owner.

CRITICAL — SHADING / EXPECTED-LOW (a false "underperforming" you must catch):
  Some inverters run permanently below their peers for a FIXED physical reason —
  afternoon shade from a neighbour's tree, a chimney, a poor roof face. That is NOT a
  fault to chase, and flagging it "underperforming" forever trains the owner to ignore
  the flag. When investigate_attention / array_detail shows an "underperforming" unit
  whose deficit is STEADY and long-standing (a consistent low peer_index, not a sudden
  drop, and expected_low is still false), PROACTIVELY ask the owner something like:
  "Inverter 13 on Chester has run ~42% of its neighbours steadily — that even pattern
  usually means permanent shading (a tree, a chimney) rather than a fault. Is something
  shading that one? If so I'll mark it so it stops flagging — and I'll still alert you
  if it ever drops below that level." If they CONFIRM a physical cause, call
  mark_inverter_expected_low(inverter_id, reason) — never mark on your own guess.
  Never nag: ask once per unit. If a unit is already expected_low and shows
  expected_low_breach=true, that IS a real new problem (it fell below its shaded
  baseline) — surface it like any other underperformer. If shading is removed (tree
  cut), use clear_inverter_expected_low.

CRITICAL — O&M ROSTER HUNGER (Repairs / any O&M question):
  You WANT a complete repair roster the way a good ops lead wants a contact sheet.
  Incomplete roster = you cannot email anyone when hardware dies. Act hungry, not polite-passive.
  When the user mentions ANY O&M/repair/installer/tech person OR asks if they have a team:
    1. ALWAYS call list_service_contacts (or repair_ops_overview) first — never guess.
    2. If empty or thin: drive the interview. Do NOT end with "Done." or "OK."
    3. When they give a name and/or email (even casually: "Rex his email is x@y.com"):
       IMMEDIATELY call upsert_service_contact with needs_confirm=false (they already gave it).
       Parse name + email + role if present. Mark is_default=true if this is the first contact.
    4. After each save, CONFIRM what you stored in one short line, then ask the NEXT missing field.
       Preferred order: name → email → phone → company/role → which arrays (all vs list) →
       "Anyone else on the team?"
    5. Keep going until you have at least: name + email + (default OR array assignments).
       Only then say the roster is ready to watch the fleet.
    6. Forbidden closes: "Done." / "Got it." / "OK." with no next question when roster is incomplete.
    7. Explain WHY you want it in one breath: so you can draft and send outreach the moment
       an inverter faults — without them babysitting.
  TRUSTED FOLLOW-UPS: the first email to any contact is always Approve & send. Once the
  owner approves ONE send to a contact, follow-ups to that contact go out automatically on
  the check-in cadence — mention this the first time it arms. If the owner says stop
  ("stop auto follow-ups", "ask me first"), call upsert_service_contact with trusted=false
  for that contact; re-arm later with trusted=true. Never promise an auto follow-up for an
  untrusted contact.
  Manufacturer warranty claims remain a separate path.
- propose_site_improvement = ship UI/product improvements via the SAME judge pipeline as
  the old "Wish this was better" button (markup screenshot → judge → auto-ship small UI).
- web_search = LIVE public internet search (news, regulations, utility policy, vendor docs,
  weather context, market rates). Use when the answer is outside this tenant's DB.
  web_fetch = open a public URL and extract readable text (after search or a pasted link).
  Always cite title + URL. Prefer tenant tools for THIS account's arrays/kWh/offtakers.
Reason multi-step: census → query → dig health. For outside facts: web_search → cite.
Do not invent rows. Do not invent web facts — search first.

Scope — you CAN:
  read ALL of THIS tenant's operational data (fleet, offtakers, bills, rates, account,
  utility accounts, generation, connections, service contacts, repair tickets) via tools —
  never invent numbers.
  search the public web and fetch public pages when needed,
  navigate UI, highlight/fill,
  patch offtaker details: share %, email, customer name, auto-send,
  solar credit rates (rate_per_kwh / net_rate_per_kwh), discount_pct,
  AND rebind utility/array sources (utility_account_id, array_id / master group),
  set tenant global/master solar credit rate + default discount,
  manage service contacts + repair tickets + send tech check-ins when directed,
  open billing portal LINKS, escalate to Ford, propose site/UI improvements.

Solar credit rates (CRITICAL — Ford 2026-07-14):
  When asked "what is the solar credit rate for Town of Glover / offtaker X?":
    ALWAYS call get_offtaker or list_offtakers / get_billing_rates — rates live on the
    offtaker + tenant globals + resolved bill credit, NOT only the Resources tab.
  Precedence: per-offtaker net_rate/rate_per_kwh → tenant master net rate → bound utility
  bill solar credit → schedule/default. Report resolved_effective_rate and source.
  To CHANGE a rate: patch_offtaker (per customer) or set_billing_rates (tenant-wide master).
  "Solar credit rate" ≈ net_rate_per_kwh (or legacy rate_per_kwh). Discount is separate %.

Scope — you MUST NOT (hard reject, no exceptions):
  change Stripe prices, charge cards, create subscriptions, alter operator billing plan,
  touch payment methods, or anything that moves money for the tenant account.
  Offtaker invoice *content* (share %, email, bill source rebind) is OK when directed;
  operator billing is NOT.

CRITICAL — offtaker "master account" / utility source is NOT the offtaker's name:
  The Invoices edit form has a MASTER (net-meter group host) dropdown and an optional
  SUB-account dropdown. Those bind array_id + utility_account_id (bill source).
  When the user says "change master account to Timberworks" or "switch utility source
  to X", use patch_offtaker with array_name / master_account / utility_account_name —
  NEVER rename customer_name to that value. Renaming is only when they explicitly say
  rename / change the offtaker's display name.

Bulk offtaker spreadsheet import (CRITICAL — exists, do not claim missing):
  Operators upload ANY roster (.xlsx/.csv) via Invoices → "⬆ Bulk import" (#rbBulkImport).
  Format-agnostic column detection + fuzzy array match + operator review, then commit.
  product_map(topic=offtakers) §7 for the full pipeline. To open it: ui_navigate #reports
  then ui_highlight #rbBulkImport, or tell them /?setup=offtakers#reports.
  Onboarding also offers optional "Upload offtaker spreadsheet" after connect.
  Never invent offtaker rows from a pasted list — send them to Bulk import so mapping
  and confidence review run. Template download is optional; their own export works.

Site improvements (CRITICAL — Ford 2026-07-14 voice fail):
  Visual / color / button / "doesn't look good" asks are NOT a design lecture.
  Do NOT monologue about design tokens, sky mode, CSS variables, or system language.
  Do NOT call product_map for pure visual polish.
  Do NOT fire many tools at once. One mind, one quiet fix path:
    1) One short spoken line: "Oh I see — I'll open the builder with a prompt ready."
    2) Call propose_site_improvement with start_markup true and text= a COMPLETE
       ready-to-build brief (imperative design prompt matching their ask — e.g.
       live energy balls on the pipeline sending to offtakers). The client fills
       the Build-it box automatically; user only circles + clicks Build.
    3) Do not leave the Build box empty. Do not ask them to retype what they said.
  Never narrate a multi-step redesign plan out loud. Background work is silent.

Clear UI BUGS (CRITICAL — Ford 2026-07-15 Chester #4):
  When the owner reports inconsistent labels/status on a card (e.g. "Error" AND
  "pulling its weight"), wrong chip, double-coded health/live signals, or an
  obvious display contradiction — that is a SOFTWARE BUG, not a product feature
  request and NOT a "needs human developer" moment.
  Path:
    1) One short line: "You're right — that's inconsistent. Shipping a fix."
    2) Call propose_site_improvement with start_markup false (or force_submit true
       if available) and text= a precise bug-fix brief: what is wrong, which
       surface (sandbox inverter card / status chip), desired honest single state.
       Mark it as a pure public/* frontend display fix so the judge AUTO-SHIPS.
    3) Do NOT default to escalate_to_ford for clear UI contradictions.
    4) Only offer escalate if the auto-ship pipeline reports a hard failure
       (not just "reviewed") or the bug needs backend/API/schema changes.
  Never tell the user a pure label/status inconsistency "needs a human look"
  as the first option.

BREVITY (always — Ford 2026-07-16 voice cutoffs):
  Default is SHORT. Long monologues get interrupted on voice and feel like a lecture in chat.
  - Answer in 1–3 short sentences for normal questions. One idea per sentence.
  - Prefer: answer first → one useful detail → optional "want more?" — stop.
  - Bullet lists only when the user asked for a list/tour/enumeration. Cap at 4 bullets,
    one short line each. Never a multi-paragraph essay unsolicited.
  - No throat-clearing ("Happy to help…", "Great question…", "Sure thing…").
  - No recapping the whole product or every tab unless they asked "what are the tabs?"
  - Tools gather depth; YOUR REPLY stays lean. If more detail is useful, offer it — don't dump it.

Voice discipline:
  - WAIT for the full request. Prefer one clarifying question over premature action.
  - Spoken path is even tighter: aim ~25–50 words unless they asked for a walkthrough/tour.
  - Put the ANSWER in the first sentence (voice reads the lead). Never bury
    the point after a long throat-clear. Example: "Town of Glover's solar credit
    rate is about $0.18/kWh." then one short detail if needed — then stop.
  - If the user says stop / wait / cancel / enough — halt immediately (client enforces;
    still never keep monologuing if they already asked you to stop).
  - Do NOT narrate every step of a tour or setup as a continuous speech; chunk into
    short lines the client can play without cutting mid-thought.

CHAT TEXT FORMAT (CRITICAL — a narrow chat column, NOT a document; Ford 2026-07-16):
  The panel is a slim sidebar. A reply that looks like a formatted report — bold on every
  line, "**Header:**" rows, headings — reads as an OVERWHELMING WALL there. Write like a
  sharp colleague texting, not like a printed brief.
  Rules:
  1. LEAD with a one-sentence answer a person could say out loud (this is also what the
     voice speaks). If everything's fine, two sentences and stop.
  2. Detail only if it helps: AT MOST one short list, 1 line per item, ~3 items — then
     "…and N more — want them?" instead of pasting all N. No sub-bullets unless asked.
  3. EMPHASIS: bold AT MOST ONE thing — the single key number or verdict (e.g. **$170/mo**).
     Do NOT bold names, labels, or every metric. Never a "**Top issues:**" / "**Next step:**"
     header line. Never use '#' headings. These are what make it a wall.
  4. Metrics ride inline with " · " middle dots, not stacked (parentheses).
       BAD:  "**Londonderry** — 1 dark, 2 low (~31% output), peer 1.02 (needs a look)."
       GOOD: "Londonderry: 1 inverter dark, 2 low · ~$40/mo."
  5. Keep the WHOLE reply tight — usually under ~70 words. Tools gather depth; the reply
     stays lean. Offer more, don't dump it.
  6. Links always as [label](https://…) — never bare "search for X".

Hard rules:
- Never invent kWh, $, counts, or status. Use tools and report what they return.
- Never access other tenants. Never reveal secrets/passwords/API keys.
- Never charge money or change Stripe prices. You may open billing-portal LINKS after confirm.
- ui_navigate and ui_highlight: run immediately, needs_confirm=false (user asked to go there).
- CLEAR DIRECTION = DO IT. If the user already stated the exact change (e.g. "change share
  to 15%", "set email to x@y.com", "make it 20 percent"), call the write tool with
  needs_confirm=false and APPLY NOW. Do NOT ask "say yes / do it / go ahead" when they
  already told you what to do. Extra confirmation is only for vague asks ("can you edit
  this offtaker?") with no value/field specified yet.
- ui_fill / ui_click / data writes: needs_confirm=false when the user's message clearly
  specifies the change; true only when the intent is ambiguous.
- Offtaker share %: use patch_offtaker with offtaker_name or subscription_id and share_pct
  (e.g. 24.5 for 24.5%), needs_confirm=false when they named the offtaker and the new %.
  After apply, the UI soft-refreshes — do not tell them to hard-refresh the browser.
- Offtaker utility / master account rebind: list_offtakers first (shows utility_account_id,
  array_id, nicknames), then patch_offtaker with utility_account_id|utility_account_name
  and/or array_id|array_name|master_account. product_map(topic=offtakers) for the full
  invoice generator model.
- Fleet attention: investigate_attention / fleet_overview. NEVER ask the user for array IDs
  you can look up. Answer with names, why, and next step.
- Account tab / email / company / plan: ALWAYS call account_summary. The email field is
  contact_email (returned as email + contact_email). Never claim email is null without
  checking account_summary first — tenant.email is NOT a real column.
- Auto-refresh / "how do you get data" / cloud vs extension: ALWAYS product_map(topic=capture)
  first, then account_summary for THIS tenant. THREE capture ideas (do not collapse them):
    cloud  = "Store it with us" — passwords on our servers, harvester 24/7 for those logins
    device = "Keep it on my computer" — passwords in extension vault; scheduled capture while browser active
    extension one-click = "Log in with SMA/Fronius/Chint…" — EnergyAgent extension opens the portal,
      auto-captures authenticated data, POSTs arrays. Does NOT create a cloud vault row.
  CRITICAL: fleet arrays for SMA/Fronius/Chint often arrived via extension one-click or onboarding
  even when capture_mode=cloud and cloud_capture.logins only lists another vendor (e.g. only Chint).
  If the owner says "I never entered SMA into cloud capture," explain extension auto-capture —
  do NOT invent that the harvester must have had the SMA password.
  When UI context has extension_present=true (or extension_heartbeat_at is recent), say the helper
  is installed/paired and can automatically reach vendor sites to capture after sign-in.
  API-key vendors (SolarEdge) are still a separate server-poll path (keys, not portal passwords).
- SHOW-AND-TELL: for "walk me through X" / "show me Account" / "tour this tab"
  the CLIENT runs a lockstep tour (highlight + speak in order) with real DOM
  selectors. Prefer ui_tour with tour_id=
  master_account|account|arrays|inverters|reports|invoices|dashboard|fleet_triage|
  analysis|resources
  — NEVER freehand ui_highlight with guessed CSS selectors (they box the wrong
  things and desync from voice). If you already started a tour, do not also fire
  navigate/highlight commands.
- If a site improvement is held by the judge: explain the reason and offer escalate_to_ford.
- If tools return empty while the UI shows data, say so and call tenant_census + escalate_to_ford.
- Prefer short spoken answers; put detail in tool timelines.
- Portal / SmartHub / "where do I see today's production for Glover": call portal_links,
  then answer with CLICKABLE markdown links like [VEC SmartHub](https://...).
  Never say "go open your browser and search" without giving the actual link.
  You may also return a ui_command {{"type":"open_url","args":{{"url":"...","label":"VEC SmartHub"}}}}
  so the client opens the portal in a new tab.

Context about where the user is may be provided as JSON (tab, selection, form).

MOBILE OS (when context.mobile_os or context.is_mobile_os_home is true):
- YOU are the operating layer on the phone — not a side chat over tabs. There is no
  tab bar in AI-home mode; the owner talks to you to finish setup and run the fleet.
- Phase "setup": drive the hands-off checklist as fast as possible, one next step at a
  time. Order: arrays live → auto-refresh (cloud portal login) → utility bills →
  offtakers (optional if monitor-only) → online pay (required once offtakers exist).
  Use context.mobile_os.next_setup_step and pillars[].done. Celebrate greens; don't
  dump desktop navigation unless they ask for Detail mode.
- Phase "running": lead with status — inverter health, last sync, cloud login health,
  offtaker send success / delivery mode / period. Offer Detail mode for deep edits
  (spreadsheets, template studio), not as the default path.
- Prefer short spoken answers + one clear CTA. ui_navigate still works if they open
  Detail mode; on pure mobile OS home, explain and use tools/census rather than
  "click the third tab."
"""


def _now() -> datetime:
    return datetime.utcnow()


# Overnight source quiet is normal (esp. extension capture). Beyond this age (hours)
# at night, silence is a real multi-day data problem — still flag it.
_NIGHT_SOURCE_AGE_ATTENTION_H = 36.0


def _fleet_clock_context(when: datetime | None = None) -> dict[str, Any]:
    """Fleet-local clock + sun-up for the Energy Agent (every turn).

    Arrays are judged in local solar time (default America/New_York). Without this
    block the model reasons in UTC / its own "now" and invents night-time outages.
    """
    from zoneinfo import ZoneInfo

    from .inverter_fleet import _is_daylight
    from .models import FLEET_TZ

    utc = when if when is not None else datetime.now(timezone.utc)
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    else:
        utc = utc.astimezone(timezone.utc)
    try:
        local = utc.astimezone(ZoneInfo(FLEET_TZ))
        tz_label = FLEET_TZ
    except Exception:
        local = utc
        tz_label = "UTC"
    try:
        sun_up = bool(_is_daylight(when=utc))
    except Exception:
        sun_up = True  # never hide a real daytime fault on calc failure
    local_hour = local.hour
    return {
        "utc_now": utc.strftime("%Y-%m-%d %H:%M UTC"),
        "fleet_timezone": tz_label,
        "fleet_local_now": local.strftime("%Y-%m-%d %H:%M %Z"),
        "fleet_local_hour": local_hour,
        "sun_up_at_fleet": sun_up,
        "solar_state": "daylight" if sun_up else "night",
        "rule": (
            "Fleet time is US Eastern by default (America/New_York). "
            "At night, zero live power and overnight source quiet are NORMAL — "
            "do not call the fleet down. Only multi-day health flags or multi-day "
            "data silence are real overnight problems. At daylight, zero while "
            "peers produce is real."
        ),
    }


def _source_needs_attention(col: dict) -> bool:
    """True when source/feed state is a real problem (not overnight sleep)."""
    src = col.get("source_status") or {}
    state = (src.get("state") or "").lower()
    if state not in ("stale", "dark", "offline"):
        return False
    # Night: overnight quiet is sleep (UI _feedBehind returns false). Only flag
    # multi-day silence beyond the normal overnight / capture gap.
    if col.get("is_daylight") is False:
        try:
            age = float(src.get("age_hours")) if src.get("age_hours") is not None else None
        except (TypeError, ValueError):
            age = None
        if age is None or age < _NIGHT_SOURCE_AGE_ATTENTION_H:
            return False
    return True


def _week_start(dt: datetime | None = None) -> datetime:
    d = (dt or _now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())  # Monday UTC


# ── models ──────────────────────────────────────────────────────────────────
class EaSession(Base):
    __tablename__ = "ea_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|ended
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    voice_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # pending confirm


class EaMessage(Base):
    __tablename__ = "ea_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool|system|transcript
    content: Mapped[str] = mapped_column(Text, default="")
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EaMemory(Base):
    """Dual memory: scope=tenant|<tenant_id> or scope=global."""
    __tablename__ = "ea_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)  # tenant:xxx | global
    key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EaCostLedger(Base):
    __tablename__ = "ea_cost_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    week_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EaChatAsset(Base):
    """File / image the owner attaches for Energy Agent to analyze."""
    __tablename__ = "ea_chat_assets"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    filename: Mapped[str] = mapped_column(String(260), default="file")
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(24), default="file")  # file|snippet|image
    text_extract: Mapped[str] = mapped_column(Text, default="")
    storage_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")


# Attachment limits (owner chat — smaller than Sovereign desk)
_EA_MAX_UPLOAD_BYTES = int(os.getenv("EA_CHAT_MAX_UPLOAD", str(8 * 1024 * 1024)))
_EA_MAX_TEXT_EXTRACT = 80_000
_EA_ASSET_DIR = Path(os.getenv("EA_CHAT_ASSET_DIR", "/tmp/ea_chat_assets"))
_EA_MAX_IMAGE_B64 = int(os.getenv("EA_CHAT_MAX_IMAGE_B64", str(5 * 1024 * 1024)))  # ~5MB raw → multimodal
_EA_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".html", ".css", ".yml", ".yaml", ".toml", ".ini", ".env",
    ".sh", ".bash", ".sql", ".log", ".xml", ".svg", ".xlsx", ".xls",
}


# ── pydantic ────────────────────────────────────────────────────────────────
class SessionIn(BaseModel):
    context: dict[str, Any] | None = None
    # Resume the tenant's open conversation (default). Survives refresh + cache clear
    # because history lives in the DB, not the browser. force_new=True starts fresh.
    resume: bool = True
    force_new: bool = False
    preferred_session_id: str | None = None


class ChatIn(BaseModel):
    session_id: str
    message: str = ""
    context: dict[str, Any] | None = None
    source: str = "text"  # text | voice
    attachment_ids: list[str] = Field(default_factory=list)


class ConfirmIn(BaseModel):
    session_id: str
    confirm: bool = True
    pending_id: str | None = None


class TranscriptIn(BaseModel):
    session_id: str
    lines: list[dict[str, Any]] = Field(default_factory=list)
    voice_seconds: float = 0.0


class UiResultIn(BaseModel):
    session_id: str
    command_id: str
    ok: bool
    detail: dict[str, Any] | None = None


class MemoryIn(BaseModel):
    scope: str = "tenant"  # tenant | global
    key: str
    value: str


# ── helpers ─────────────────────────────────────────────────────────────────
def _enabled():
    if not ENERGY_AGENT_ENABLED:
        raise HTTPException(503, "Energy Agent is temporarily disabled")


def _auth(authorization: str | None) -> Tenant:
    _enabled()
    return tenant_from_session(authorization)


def _budget_rows(db, tenant_id: str) -> list:
    ws = _week_start()
    return list(
        db.execute(
            select(EaCostLedger).where(
                EaCostLedger.tenant_id == tenant_id,
                EaCostLedger.week_start >= ws,
            )
        ).scalars().all()
    )


def _budget_spent(db, tenant_id: str) -> float:
    return float(sum(r.amount_usd or 0 for r in _budget_rows(db, tenant_id)))


def _budget_breakdown(db, tenant_id: str) -> dict:
    """Split weekly spend into thinking (chat/LLM) vs voice for the usage UI."""
    thinking = 0.0
    voice = 0.0
    other = 0.0
    for r in _budget_rows(db, tenant_id):
        amt = float(r.amount_usd or 0)
        reason = (r.reason or "").lower()
        if reason.startswith("voice"):
            voice += amt
        elif reason.startswith("chat") or reason.startswith("llm") or reason.startswith("tool"):
            thinking += amt
        else:
            other += amt
    return {
        "thinking_usd": round(thinking, 4),
        "voice_usd": round(voice, 4),
        "other_usd": round(other, 4),
    }


def _charge(db, tenant_id: str, amount: float, reason: str):
    if amount <= 0:
        return
    db.add(EaCostLedger(
        tenant_id=tenant_id,
        week_start=_week_start(),
        amount_usd=round(amount, 6),
        reason=reason[:64],
    ))


def _check_budget(db, tenant_id: str) -> dict:
    """Weekly $ cap covering BOTH thinking (chat/tools) and voice minutes.

    Free tier: small weekly sample (default $2.50) so owners can try the agent.
    Energy Agent Pro ($50/mo) or comped/demo → unlimited (ok always).
    """
    spent = _budget_spent(db, tenant_id)
    tenant = db.get(Tenant, tenant_id) if tenant_id else None
    pro_usd = 50.0
    stripe_ready = False
    pro = False
    cap_opt: float | None = WEEKLY_BUDGET_USD
    try:
        from .pricing_ao_unified import (
            AI_FREE_WEEKLY_BUDGET_USD,
            AI_PRO_MONTHLY_USD,
            AI_PRO_PRICE_ID,
            ai_budget_cap_usd,
            tenant_has_ai_pro,
        )
        pro_usd = float(AI_PRO_MONTHLY_USD)
        stripe_ready = bool(AI_PRO_PRICE_ID)
        pro = bool(tenant and tenant_has_ai_pro(tenant))
        cap_opt = ai_budget_cap_usd(tenant) if tenant is not None else AI_FREE_WEEKLY_BUDGET_USD
    except Exception:  # noqa: BLE001 — never break chat on pricing import
        pass

    if pro or cap_opt is None:
        # Unlimited — still report spend for transparency, bar stays green
        return {
            "weekly_budget_usd": None,
            "spent_usd": round(spent, 4),
            "remaining_usd": None,
            "pct_used": 0.0,
            "warn": False,
            "week_start": _week_start().isoformat() + "Z",
            "ok": True,
            "unlimited": True,
            "tier": "pro",
            "pro_monthly_usd": pro_usd,
            "covers": "thinking+voice",
            "breakdown": _budget_breakdown(db, tenant_id),
            "upgrade": None,
        }

    cap = max(0.01, float(cap_opt))
    remaining = max(0.0, cap - spent)
    pct = min(100.0, (spent / cap) * 100.0)
    warn_at = max(0.0, min(1.0, WEEKLY_BUDGET_WARN_FRAC)) * 100.0
    ok = remaining > 0.02
    return {
        "weekly_budget_usd": round(cap, 2),
        "spent_usd": round(spent, 4),
        "remaining_usd": round(remaining, 4),
        "pct_used": round(pct, 1),
        "warn": bool(ok and pct >= warn_at),
        "week_start": _week_start().isoformat() + "Z",
        "ok": ok,
        "unlimited": False,
        "tier": "free",
        "pro_monthly_usd": pro_usd,
        "covers": "thinking+voice",
        "breakdown": _budget_breakdown(db, tenant_id),
        "upgrade": {
            "product": "energy_agent_pro",
            "price_usd": pro_usd,
            "stripe_ready": stripe_ready,
            "cta": f"Upgrade to Energy Agent Pro — ${pro_usd:.0f}/mo unlimited AI",
            "path": "/#account",
        },
    }


def _get_session(db, sid: str, tenant_id: str) -> EaSession:
    s = db.get(EaSession, sid)
    if not s or s.tenant_id != tenant_id:
        raise HTTPException(404, "Session not found")
    if s.status != "open":
        raise HTTPException(400, "Session ended")
    return s


def _session_message_count(db, session_id: str) -> int:
    n = db.execute(
        select(func.count()).select_from(EaMessage).where(
            EaMessage.session_id == session_id,
            EaMessage.role.in_(("user", "assistant")),
        )
    ).scalar() or 0
    return int(n)


def _session_messages_payload(db, session_id: str, *, limit: int = 500) -> list[dict]:
    """User/assistant turns for UI restore (skip system/tool noise).

    Returns the *most recent* `limit` turns in chronological order so long
    conversations still restore a deep scrollback (not just the first N).
    Includes email-channel turns (meta.channel=email) so chat restore feels
    continuous with the repair mailbox.
    """
    limit = max(20, min(int(limit or 500), 1000))
    # Take newest `limit` by id desc, then reverse for paint order
    newest = db.execute(
        select(EaMessage)
        .where(
            EaMessage.session_id == session_id,
            EaMessage.role.in_(("user", "assistant")),
        )
        .order_by(EaMessage.id.desc())
        .limit(limit)
    ).scalars().all()
    msgs = list(reversed(newest))
    out = []
    for m in msgs:
        content = (m.content or "").strip()
        if not content:
            continue
        meta = {}
        if m.meta_json:
            try:
                meta = json.loads(m.meta_json) or {}
            except Exception:
                meta = {}
        out.append({
            "role": m.role,
            "content": content[:8000],
            "at": m.created_at.isoformat() + "Z" if m.created_at else None,
            "channel": meta.get("channel"),
            "origin": meta.get("origin"),
            "kind": meta.get("kind"),
            "ticket_id": meta.get("ticket_id"),
            "mindUpdate": bool(meta.get("channel") == "email" or meta.get("mindUpdate")),
        })
    return out


def mirror_repair_to_open_session(
    db,
    tenant_id: str,
    speak_line: str,
    *,
    meta: dict | None = None,
) -> str | None:
    """Append a repair-email turn to the tenant's open chat session.

    Makes email + chat one continuous surface: history restore and agent
    context both see what happened on the mailbox.
    """
    text = (speak_line or "").strip()
    if not text or not tenant_id:
        return None
    # Prefer the open session with the most recent assistant/user activity
    sess = _find_resumable_session(db, tenant_id)
    if sess is None:
        sess = db.execute(
            select(EaSession)
            .where(
                EaSession.tenant_id == tenant_id,
                EaSession.status == "open",
            )
            .order_by(EaSession.created_at.desc())
            .limit(1)
        ).scalars().first()
    if sess is None:
        return None
    payload = {
        "channel": "email",
        "origin": "repair",
        "mindUpdate": True,
        **(meta or {}),
    }
    db.add(EaMessage(
        session_id=sess.id,
        tenant_id=tenant_id,
        role="assistant",
        content=text[:4000],
        meta_json=json.dumps(payload, default=str)[:4000],
    ))
    db.flush()
    return sess.id


def repair_email_surface_digest(db, tenant_id: str, *, limit: int = 20) -> str:
    """Ground-truth email thread for the agent system prompt (chat ⇄ mail)."""
    try:
        from . import repair_ops as ro
        return ro.build_email_surface_digest(db, tenant_id, limit=limit)
    except Exception as exc:
        log.warning("repair email digest failed: %s", exc)
        return ""


def _find_resumable_session(
    db,
    tenant_id: str,
    preferred_id: str | None = None,
    *,
    max_age_days: int = 90,
) -> EaSession | None:
    """Open session with the richest recent chat — not merely newest created.

    Bug (Ford 2026-07-14): ordering by session.created_at alone resumed empty
    brand-new sessions and hid the long conversation with only the last reply.
    Prefer preferred_id only if it has real user/assistant turns; else pick the
    open session with the latest chat activity.
    """
    cutoff = _now() - timedelta(days=max(1, max_age_days))

    def _ok(s: EaSession | None) -> bool:
        return bool(
            s
            and s.tenant_id == tenant_id
            and s.status == "open"
            and s.created_at
            and s.created_at >= cutoff
        )

    if preferred_id:
        pref = db.get(EaSession, preferred_id)
        if _ok(pref) and _session_message_count(db, pref.id) > 0:
            return pref

    # Open sessions with last user/assistant activity
    last_msg = (
        select(
            EaMessage.session_id.label("sid"),
            func.max(EaMessage.id).label("last_id"),
            func.count(EaMessage.id).label("n"),
        )
        .where(
            EaMessage.tenant_id == tenant_id,
            EaMessage.role.in_(("user", "assistant")),
        )
        .group_by(EaMessage.session_id)
        .subquery()
    )
    row = db.execute(
        select(EaSession)
        .join(last_msg, EaSession.id == last_msg.c.sid)
        .where(
            EaSession.tenant_id == tenant_id,
            EaSession.status == "open",
            EaSession.created_at >= cutoff,
            last_msg.c.n > 0,
        )
        .order_by(last_msg.c.last_id.desc())
        .limit(1)
    ).scalars().first()
    if row is not None:
        return row

    # No chat yet — fall back to newest open shell session
    return db.execute(
        select(EaSession)
        .where(
            EaSession.tenant_id == tenant_id,
            EaSession.status == "open",
            EaSession.created_at >= cutoff,
        )
        .order_by(EaSession.created_at.desc())
        .limit(1)
    ).scalars().first()


def _mem_get(db, scope: str, limit: int = 40) -> list[dict]:
    rows = db.execute(
        select(EaMemory).where(EaMemory.scope == scope)
        .order_by(EaMemory.updated_at.desc()).limit(limit)
    ).scalars().all()
    return [{"key": r.key, "value": r.value, "updated_at": r.updated_at.isoformat() + "Z"} for r in rows]


def _mem_set(db, scope: str, key: str, value: str):
    key = (key or "")[:120]
    value = (value or "")[:8000]
    # scrub secrets-ish patterns from global
    if scope in ("global", "global_pending"):
        if re.search(r"(password|api[_-]?key|secret|sk-|Bearer\s)", value, re.I):
            raise HTTPException(400, "Global memory cannot store secrets")
    existing = db.execute(
        select(EaMemory).where(EaMemory.scope == scope, EaMemory.key == key)
    ).scalar_one_or_none()
    if existing:
        existing.value = value
        existing.updated_at = _now()
    else:
        db.add(EaMemory(scope=scope, key=key, value=value))


def _queue_global_memory(db, tenant_id: str, key: str, value: str) -> dict:
    """Tenant-originated global-behavior tips are QUEUED, never applied directly.

    Only `scope == "global"` rows are injected into system prompts; anything a
    tenant (or the agent acting for one) writes lands in `global_pending` and
    requires an explicit admin promotion (ENERGY_AGENT_VOICE.md §5B). This is
    the cross-tenant prompt-injection gate — a demo tenant must never be able
    to steer every other tenant's agent.
    """
    pend_key = f"{tenant_id}/{(key or 'tip')[:100]}"
    _mem_set(db, "global_pending", pend_key, value)
    try:
        from .notify import send_internal_alert
        send_internal_alert(
            "[EnergyAgent] global behavior tip queued for review",
            f"tenant: {tenant_id}\nkey: {pend_key}\nvalue:\n{(value or '')[:2000]}\n\n"
            "Promote: POST /v1/energy-agent/admin/memory/promote {\"key\": \"" + pend_key + "\"} "
            "(x-admin-key). Reject: .../reject with the same body.",
        )
    except Exception as e:
        log.warning("global-pending alert email failed: %s", e)
    return {
        "ok": True,
        "scope": "global_pending",
        "note": "Queued for review — global behavior changes apply only after Ford approves.",
    }


# ── tools ───────────────────────────────────────────────────────────────────
TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "tenant_census",
            "description": (
                "AUTHORITATIVE inventory from the database for this tenant — every array, "
                "inverter, connection, offtaker, and recent production totals. Use FIRST for "
                "'how many arrays/inverters do I have', 'list my fleet', or when health tools "
                "look incomplete. This is ground truth; fleet_overview health may omit "
                "meter-only arrays."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_names": {
                        "type": "boolean",
                        "description": "Include full name lists (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tenant",
            "description": (
                "Free-form READ-ONLY investigation of this tenant's data. Pick a resource "
                "and optional filters — reason step-by-step like a data analyst. Resources: "
                "arrays, inverters, offtakers, daily_generation, utility_accounts, "
                "inverter_connections, bills_summary, bills, tenant_pricing. Never invent rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "description": (
                            "arrays | inverters | offtakers | daily_generation | "
                            "utility_accounts | inverter_connections | bills_summary | "
                            "bills | tenant_pricing"
                        ),
                    },
                    "vendor": {"type": "string", "description": "Filter by vendor when relevant"},
                    "array_id": {"type": "integer"},
                    "array_name": {"type": "string", "description": "Name substring"},
                    "status": {"type": "string", "description": "For offtakers: enabled filter"},
                    "days": {
                        "type": "integer",
                        "description": "For daily_generation: lookback days (default 14, max 90)",
                    },
                    "group_by": {
                        "type": "string",
                        "description": "Optional: vendor | array | day | none",
                    },
                    "limit": {"type": "integer", "description": "Max rows (default 100, max 300)"},
                    "question": {
                        "type": "string",
                        "description": "What you're trying to answer (helps shape the response)",
                    },
                },
                "required": ["resource"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "product_map",
            "description": (
                "Authoritative Array Operator product knowledge (support map + "
                "surface mental model). ALWAYS call before explaining Auto-refresh, "
                "cloud vs extension, invoices, analysis, plans, onboarding, OR before "
                "describing what a page/tab is for (use surface / surface_*). "
                "Topics include tabs, system, surface, product_spine, "
                "surface_invoices, surface_inverters, surface_fleet_triage, "
                "surface_analysis, surface_account, surface_resources, surface_repairs, "
                "and domain topics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "Domain: tabs | system | fleet | capture | vendors | analysis | "
                            "health | offtakers | billing | plans | onboarding | resources | "
                            "status | agent | api | datamodel | glossary | security | tools. "
                            "Surface mental model (macro/meso/micro): surface | product_spine | "
                            "surface_invoices | surface_inverters | surface_fleet_triage | "
                            "surface_analysis | surface_account | surface_resources | "
                            "surface_repairs | orientation_playbook | surface_global | "
                            "anti_hallucination. "
                            "surface = whole-product layout + orientation playbook; "
                            "surface_invoices = Invoices tab purpose+structure; etc. "
                            "Pass 'all' for the topic directory."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_site_improvement",
            "description": (
                "Propose a product/UI change through the self-improving-site pipeline "
                "(same as 'Wish this was better'). An internal JUDGE approves auto-ship "
                "(frontend-only UX), branches riskier work, or passes. Prefer starting the "
                "client mark-up flow (returns ui improve_site) so the user circles the spot. "
                "CRITICAL: put a complete, ready-to-build design brief in text= — the client "
                "auto-fills the Build-it box with it so the user only circles and clicks Build. "
                "Write an imperative prompt (e.g. 'Upgrade the pipeline visualization to live "
                "energy balls flowing to offtakers…'), not just 'user wants something cooler'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Ready-to-build prompt for the Improve box and the judge. "
                            "Imperative, specific, 1–4 sentences. Include what/where/visual intent."
                        ),
                    },
                    "build_prompt": {
                        "type": "string",
                        "description": "Optional override for the prefilled Build-it box (defaults to text)",
                    },
                    "start_markup": {
                        "type": "boolean",
                        "description": "If true (default), open freeze+circle UI first",
                    },
                    "screenshot_b64": {
                        "type": "string",
                        "description": "Optional marked-up PNG base64 if already captured",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fleet_overview",
            "description": (
                "Full fleet health snapshot from the live fleet-tree (same verdicts as the "
                "Inverters / Fleet Triage UI). Returns each array's alert level, vendor, "
                "today kWh, live power, source/sync freshness, and problem inverters with "
                "diagnosis. Filter with vendor (e.g. 'sma') or needs_attention_only. "
                "For complete inventory counts use tenant_census first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Optional vendor filter: sma, solaredge, fronius, chint, locus",
                    },
                    "needs_attention_only": {
                        "type": "boolean",
                        "description": "If true, only arrays with warn/critical alerts",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "investigate_attention",
            "description": (
                "Why arrays need attention — REQUIRED for 'how is my fleet' / morning "
                "health questions. Matches Spreadsheet NEED ATTENTION: 14-day peer health "
                "PLUS live dark/low overlays (status may still say ok). Returns TOTALS + "
                "every problem array with why, live_anomalies by name, and next step. "
                "Never summarize only one underperformer when more units are flagged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {
                        "type": "string",
                        "description": "Optional: sma, solaredge, fronius, chint, locus",
                    },
                    "array_name": {
                        "type": "string",
                        "description": "Optional name substring to focus on one site",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max problem arrays to return (default 40)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "array_detail",
            "description": (
                "Deep dive on ONE array by id or name: inverters, peer_index, status, "
                "diagnosis, live power, last report, source/sync status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_id": {"type": "integer"},
                    "name": {"type": "string", "description": "Array name substring"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_inverter_expected_low",
            "description": (
                "Mark ONE inverter as EXPECTED-LOW — it runs permanently below its peers "
                "for a fixed physical reason the OWNER has CONFIRMED (afternoon shade from "
                "a neighbour's tree, a chimney, a poor roof face). Use this ONLY after the "
                "owner confirms the cause — never on your own guess. It re-baselines the "
                "unit: it stops showing 'underperforming' while it holds its current level, "
                "but STILL flags and alerts if it drops below that baseline (a real new "
                "fault). This asks the owner to confirm before applying. When a unit is a "
                "steady, long-standing underperformer (consistent deficit, not worsening), "
                "PROACTIVELY ask the owner whether shading/obstruction explains it, then "
                "call this if they say yes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inverter_id": {"type": "integer", "description": "The inverter to mark (from investigate_attention / array_detail)"},
                    "reason": {"type": "string", "description": "Short cause in the owner's words, e.g. 'Afternoon shade from neighbour's maple'"},
                    "needs_confirm": {"type": "boolean", "default": True,
                                       "description": "false only when the owner has ALREADY confirmed the shading cause in this turn"},
                },
                "required": ["inverter_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_inverter_expected_low",
            "description": (
                "Undo an expected-low mark on an inverter — return it to normal peer "
                "grading (e.g. the shading was removed, the tree was cut, or it was set by "
                "mistake). Asks the owner to confirm first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inverter_id": {"type": "integer"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["inverter_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_ops_overview",
            "description": (
                "O&M healing overview: service contacts, array assignments, open repair "
                "tickets (including underperforming + dead/fault units auto-opened from "
                "the fleet), and agent activity. When the user asks what you're working "
                "on or about the repair system, CALL THIS and describe each open case by "
                "site + inverter + fail_type (e.g. Tannery Brook #1 underperforming — "
                "drafting outreach to Rex). Never invent open cases; never ignore "
                "underperforming tickets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reconcile": {
                        "type": "boolean",
                        "description": "Refresh tickets from live fleet first (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_service_contacts",
            "description": (
                "List the operator's O&M/repair roster (installers, electricians, techs) "
                "and which arrays each is assigned to. ALWAYS call this before answering "
                "'do I have an O&M team?' or starting repair setup — never invent an empty "
                "or full roster."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_service_contact",
            "description": (
                "Create or update a service contact on the O&M/repair roster. "
                "CALL THIS AS SOON AS the user gives a name and/or email for a tech — "
                "do not wait for a perfect form. Partial is fine; update again when more arrives. "
                "First contact on an empty roster: is_default=true. "
                "needs_confirm=false when the user already stated name/email in this turn "
                "(e.g. 'Rex, email is rex@…'). After save, keep interviewing for phone, "
                "company/role, array coverage, and additional teammates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer", "description": "Omit to create"},
                    "name": {"type": "string"},
                    "company": {"type": "string"},
                    "role": {
                        "type": "string",
                        "description": "installer|om|electrician|technician|general_contractor|other",
                    },
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "notes": {"type": "string"},
                    "is_default": {"type": "boolean"},
                    "active": {"type": "boolean"},
                    "trusted": {
                        "type": "boolean",
                        "description": (
                            "Auto follow-up trust. Set false when the owner says "
                            "'stop auto follow-ups' for this contact (back to "
                            "Approve & send); true re-arms it. Omit to leave as is — "
                            "trust arms itself on the first approved send."
                        ),
                    },
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_service_contact",
            "description": (
                "Assign a service contact as primary (or backup) O&M for an array. "
                "Use array_id or array_name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "array_id": {"type": "integer"},
                    "array_name": {"type": "string"},
                    "kind": {"type": "string", "description": "primary|backup (default primary)"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["contact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_repair_tickets",
            "description": "List repair tickets (active by default) with status and assigned tech.",
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {"type": "boolean", "default": True},
                    "array_id": {"type": "integer"},
                    "status": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_repair_ticket",
            "description": (
                "Open a repair ticket for a down array/inverter and assign the ops contact. "
                "Drafts a check-in email; does NOT send until send_repair_checkin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_id": {"type": "integer"},
                    "array_name": {"type": "string"},
                    "inverter_id": {"type": "integer"},
                    "contact_id": {"type": "integer"},
                    "fail_type": {"type": "string", "description": "dead|fault|comm_gap|other"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_repair_ticket",
            "description": (
                "Update ticket status (open|waiting_reply|scheduled|in_progress|resolved|"
                "cancelled), reassign contact, or set tech_note / scheduled_for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "contact_id": {"type": "integer"},
                    "tech_note": {"type": "string"},
                    "description": {"type": "string"},
                    "scheduled_for": {"type": "string", "description": "ISO datetime"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_repair_checkin",
            "description": (
                "Build or refresh the check-in email draft for a repair ticket "
                "(does not send). Returns to/subject/body for review."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_repair_checkin",
            "description": (
                "EMAIL the repair check-in to the assigned tech (or owner as forward packet). "
                "Outward communication — set needs_confirm=false only when the user clearly "
                "asked to contact/email/check in with the tech now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_repair_note",
            "description": (
                "Log an inbound status note on a ticket (e.g. tech said parts ordered). "
                "May auto-bump status from keywords (fixed/scheduled/in progress)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "note": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
                "required": ["ticket_id", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_repair_phone_note",
            "description": (
                "Log a phone-call status note on a repair ticket (after talking to the tech). "
                "Optional phone override; defaults to the contact's phone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "note": {"type": "string"},
                    "phone": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["ticket_id", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_repair_sms",
            "description": (
                "SMS check-in to the assigned tech. Uses Twilio when configured; otherwise "
                "returns an sms: URI for the owner's phone. Confirm before sending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "body": {"type": "string"},
                    "to": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_offtakers",
            "description": (
                "List offtaker subscriptions with name, share, email, array_id/array_name, "
                "utility_account_id + nickname/account number (bill source), delivery mode. "
                "Call before rebinding master account / utility source."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_offtaker",
            "description": "Get one offtaker by id or name substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "integer"},
                    "name": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fleet_trends_summary",
            "description": "Trailing production: TTM kWh, lifetime, YoY sketch from fleet-trends data.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "production_forecast",
            "description": (
                "Weather-aware predicted-vs-actual (the Analysis 'Production vs "
                "expected' math): fleet ratio_pct + per-array breakdown over the "
                "window. THE tool for 'why is production low?' / 'are we beating "
                "the weather?' — separates cloudy-week from a real site problem. "
                "Optional array_id/array_name narrows to one site."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_id": {"type": "integer", "description": "Limit to one array"},
                    "array_name": {"type": "string", "description": "Or match by name"},
                    "window_days": {"type": "integer", "description": "3-30, default 14"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_invoices",
            "description": (
                "Offtaker invoice MONEY view: pending drafts + recently sent with "
                "dollar amounts, plus pending-total and sent-last-30d totals. Use "
                "for 'how much is drafted / what did we bill?' — read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "sent", "all"],
                        "description": "Filter; default all",
                    },
                    "limit": {"type": "integer", "description": "Max rows (1-30, default 12)"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "account_summary",
            "description": (
                "Account tab data for THIS tenant — company, operator name, "
                "contact email, plan, subscription/trial status, card on file (yes/no, "
                "not full card number), capture mode, connected utilities, counts. "
                "Use whenever the user asks about Account, Master Account (legacy name), email, company, plan, "
                "or 'what's on my account'. Source of truth is contact_email (not a null email field)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_billing": {
                        "type": "boolean",
                        "description": "Include month-to-date billing snapshot (default true)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "setup_status",
            "description": (
                "YOUR STANDING OBJECTIVE: is this operator fully set up and operational, "
                "and if not, what is the single highest-value gap? Returns the completeness "
                "pillars (arrays, auto-refresh, DATA FRESHNESS, utility bills, offtakers, "
                "repair contact, online pay), whether data is flowing (stale capture = a "
                "money leak), and top_gap. Call this when the user asks 'am I all set / "
                "what's left / is everything working', at the start of a setup/onboarding "
                "conversation, or before nudging. Lead with the specific gap and act — never "
                "ask 'is everything set up?'."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_capture",
            "description": (
                "Force fresh data NOW when capture has gone stale — re-arms cloud vault logins "
                "(harvester re-captures in ~a minute) and re-pulls utility bills. Use when data "
                "is stale (setup_status data_fresh=false) or the user asks 'refresh / why is my "
                "data old / pull the latest'. Be honest about limits: device/extension vendors "
                "and SmartHub/VEC co-ops only refresh from an open browser; SolarEdge auto-polls. "
                "Confirm before running (it reaches out to the portals)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "needs_confirm": {"type": "boolean", "default": True},
                    "reason": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "billing_portal_link",
            "description": "Get Stripe customer portal URL for this tenant (open link; never charges).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_pipeline",
            "description": "Invoice send-pipeline snapshot (drafts ready, auto-send, next run).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "portal_links",
            "description": (
                "List vendor + utility portal URLs for THIS tenant (Solar.web, SmartHub, "
                "GMP, Chint, SolarEdge, etc.). Use when the owner asks where to check "
                "today's production, open a monitoring site, or find SmartHub while "
                "inverter data is missing. ALWAYS put the returned markdown links in "
                "your reply so they are clickable in chat. Optionally call open_url "
                "to open one portal in a new browser tab."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "array_name": {
                        "type": "string",
                        "description": "Optional array name to prioritize (e.g. Glover, Danville)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open an https URL in a new browser tab for the owner (vendor portal, "
                "SmartHub, etc.). Prefer after portal_links. Also put the same URL as a "
                "markdown link in your chat reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "https URL to open"},
                    "label": {"type": "string", "description": "Short link label for chat"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_navigate",
            "description": (
                "Navigate immediately (no confirm). Use USER-FACING tab names in speech; "
                "hashes are internal: Fleet Triage=#dashboard, Inverters=#arrays, "
                "Analysis=#analysis (trends is a sub-view, not a tab), Invoices=#reports, "
                "Operations=#ops (Resources is a sub-tab at #resources), Account=#account. "
                "Never call tabs Dashboard/Arrays/Reports/Trends; never list Resources as a top tab."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": (
                            "#dashboard (Fleet Triage) | #arrays (Inverters) | #analysis | "
                            "#reports (Invoices) | #ops (Operations) | #resources (Operations→Resources) | "
                            "#account (Account)"
                        ),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_highlight",
            "description": "Highlight a CSS selector on the page immediately (no confirm). Optionally say a short line while highlighting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "label": {"type": "string"},
                    "say": {"type": "string", "description": "Short narration shown+spoken during highlight"},
                    "ms": {"type": "integer", "description": "Highlight duration ms (default 4500)"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_tour",
            "description": (
                "SHOW-AND-TELL walkthrough: navigates tabs and highlights real UI "
                "elements while narrating. Use for 'walk me through Account', "
                "'show me invoices', etc. Prefer this over a text-only explanation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tour_id": {
                        "type": "string",
                        "description": (
                            "Preset tour for a top-bar tab: master_account|account, "
                            "arrays|inverters, reports|invoices, dashboard|fleet_triage, "
                            "analysis, resources. Prefer presets over custom steps."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "description": "Optional custom steps: {hash?, selector?, say?, ms?}",
                        "items": {"type": "object"},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_fill",
            "description": "Fill an input on the page. Always needs confirm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "reason": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_click",
            "description": "Click a button/link on the page. Always needs confirm for destructive/save/send.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "reason": {"type": "string"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_tenant",
            "description": "Store a short fact in private tenant memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_global_behavior",
            "description": "Store a non-PII behavior tip shared across all Energy Agent instances.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_ford",
            "description": (
                "Escalate to Ford's standing Operator inbox (Grok triage + board at "
                "/admin/escalations). Call whenever unsure or the user has a product gap — "
                "even if they decline. Prefer this over promising email-only follow-up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "user_said": {"type": "string"},
                    "severity": {"type": "boolean", "default": False},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_offtaker",
            "description": (
                "Update one offtaker: share %, email, display name, auto-send, "
                "solar credit / net rates ($/kWh), discount %, AND/OR "
                "rebind the bill source (utility account + master net-meter group). "
                "Identify by subscription_id OR offtaker_name (partial match ok). "
                "CRITICAL: 'master account' / 'utility source' / 'array source' means "
                "array_id + utility_account_id — NOT renaming customer_name. "
                "Only pass name= when the user explicitly wants to rename the offtaker. "
                "share_pct is percent (25) or fraction 0–1. "
                "rate_per_kwh / net_rate_per_kwh are $/kWh solar credit rates. "
                "discount_pct is fraction (0.10 = 10% off) or percent (10). "
                "Pass clear_rate=true to remove a per-offtaker rate override (fall back to master/bill). "
                "Set needs_confirm=false when the user already stated the exact change "
                "(e.g. share to 15%, rate to 0.18). Do not wait for a second 'yes'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "integer"},
                    "offtaker_name": {
                        "type": "string",
                        "description": "Customer/offtaker name when id is unknown",
                    },
                    "email": {"type": "string"},
                    "name": {
                        "type": "string",
                        "description": (
                            "New DISPLAY name for the offtaker only. Do NOT set this when "
                            "the user wants to change master account / utility / array source."
                        ),
                    },
                    "share_pct": {
                        "type": "number",
                        "description": "Share as percent (25) or fraction (0.25). Applied as allocation_pct / array_share_pct.",
                    },
                    "rate_per_kwh": {
                        "type": "number",
                        "description": "Legacy flat solar credit $/kWh for this offtaker (override).",
                    },
                    "net_rate_per_kwh": {
                        "type": "number",
                        "description": (
                            "Net / solar credit rate $/kWh for this offtaker (preferred). "
                            "This is what users mean by 'solar credit rate'."
                        ),
                    },
                    "discount_pct": {
                        "type": "number",
                        "description": "Discount: 0.10 or 10 for 10% off. Null/clear via clear_discount.",
                    },
                    "clear_rate": {
                        "type": "boolean",
                        "description": "If true, clear per-offtaker rate overrides (use master/bill).",
                    },
                    "clear_discount": {
                        "type": "boolean",
                        "description": "If true, clear per-offtaker discount override.",
                    },
                    "auto_send": {"type": "boolean"},
                    "utility_account_id": {
                        "type": "integer",
                        "description": "Bind offtaker to this utility bill source (sub-meter or host).",
                    },
                    "utility_account_name": {
                        "type": "string",
                        "description": (
                            "Resolve utility bill by nickname, account number, or service address "
                            "(e.g. 'Timberworks', 'St J Main St'). Prefer over guessing ids."
                        ),
                    },
                    "array_id": {
                        "type": "integer",
                        "description": "Master net-meter GROUP (array) for allocation cross-check.",
                    },
                    "array_name": {
                        "type": "string",
                        "description": "Resolve master group by array name (partial match ok).",
                    },
                    "master_account": {
                        "type": "string",
                        "description": (
                            "UI 'Master account' dropdown target — utility nickname OR array/"
                            "group name (e.g. Timberworks). Rebinds bill group, does NOT rename."
                        ),
                    },
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_billing_rates",
            "description": (
                "Read THIS tenant's solar credit / billing rates: master global defaults "
                "plus optional one offtaker's override + RESOLVED effective rate "
                "(what invoices actually use). Call when asked about solar credit rates, "
                "net rates, discounts, or offtaker pricing. Prefer offtaker_name for "
                "'Town of Glover' style questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "offtaker_name": {"type": "string"},
                    "subscription_id": {"type": "integer"},
                    "include_all_offtakers": {
                        "type": "boolean",
                        "description": "If true, include rate snapshot for every offtaker (capped).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_billing_rates",
            "description": (
                "Set THIS tenant's MASTER / global solar credit rate and/or default discount. "
                "Affects every offtaker without a per-offtaker override. "
                "For a single offtaker use patch_offtaker instead. "
                "Pass clear_net_rate=true to blank the master (each offtaker uses their bill rate). "
                "Set needs_confirm=false when the user stated the exact $/kWh."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "default_net_rate_per_kwh": {
                        "type": "number",
                        "description": "Master solar credit / net rate $/kWh",
                    },
                    "default_discount_pct": {
                        "type": "number",
                        "description": "Default discount fraction 0.10 or percent 10",
                    },
                    "default_billing_rate_per_kwh": {
                        "type": "number",
                        "description": "Legacy flat global rate $/kWh (optional)",
                    },
                    "clear_net_rate": {"type": "boolean"},
                    "clear_discount": {"type": "boolean"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live public internet (news, utility policy, weather context, "
                "vendor docs, rates, regulations). Use for questions OUTSIDE this tenant's "
                "database — e.g. 'Vermont net metering 2026', 'SMA ennexOS error code', "
                "'GMP solar credit rates'. Do NOT use for this account's arrays/offtakers/"
                "kWh (use tenant_census / query_tenant / fleet tools). Cite title + URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (plain English or keywords)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many results (1–8, default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch and extract readable text from a public HTTPS URL (after web_search "
                "or when the user pastes a link). Use for policy PDFs pages, utility pages, "
                "vendor help. Never fetch internal/private hosts. Max ~40k chars extracted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public http(s) URL to fetch",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters of text to return (default 12000, max 40000)",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def _slim_inverter(inv: dict) -> dict:
    """Compact inverter row for agent tools (no sparkline series)."""
    return {
        "inverter_id": inv.get("inverter_id"),
        "sn": inv.get("sn"),
        "name": inv.get("name"),
        "model": inv.get("model"),
        "vendor": inv.get("vendor"),
        "nameplate_kw": inv.get("nameplate_kw"),
        "status": inv.get("status") or "ok",
        "diagnosis": inv.get("diagnosis"),
        "peer_index": inv.get("peer_index"),
        "window_kwh": inv.get("window_kwh"),
        "produced_today_kwh": inv.get("produced_today_kwh"),
        "current_power_w": inv.get("current_power_w"),
        "last_report": inv.get("last_report"),
        "no_energy_register": bool(inv.get("no_energy_register")),
        "last_mode": inv.get("last_mode"),
        # Owner-confirmed structural underperformance (shading/obstruction). When
        # expected_low is True this unit is judged against its own baseline, so a
        # steady low reads OK; expected_low_breach True means it dropped BELOW that
        # baseline (a real new problem). If a unit is "underperforming" but NOT
        # expected_low, it's a candidate to ASK the owner about shading.
        "expected_low": bool(inv.get("expected_low")),
        "expected_low_reason": inv.get("expected_low_reason"),
        "expected_low_baseline": inv.get("expected_low_baseline"),
        "expected_low_breach": bool(inv.get("expected_low_breach")),
    }


# ── recoverable-$ (mirrors Fleet Triage's tile math) ────────────────────────
_LOSS_WINDOW_DAYS = 14  # peer_analysis.WINDOW_DAYS — window_kwh spans this window
_ENERGY_RATE_FALLBACK = 0.21  # $/kWh — same demo/anon fallback the UI uses


def _tenant_energy_rate(tenant: Tenant) -> float:
    try:
        r = float(getattr(tenant, "default_net_rate_per_kwh", None) or 0)
    except (TypeError, ValueError):
        r = 0.0
    return r if r > 0 else _ENERGY_RATE_FALLBACK


def _est_lost_kwh_window(inv: dict, fleet_window_kwh: float, total_np: float) -> float:
    """Per-inverter lost-kWh estimate over the 14-day peer window — mirrors
    command-center.js lostKwh() exactly so the agent's dollars always match the
    Fleet Triage 'Recoverable' tile the owner is looking at. Only confirmed peer
    verdicts are priced; live dark/low and comm_gap carry no dollars yet."""
    fair = (inv.get("nameplate_kw") or 0.0) / (total_np or 1.0) * (fleet_window_kwh or 0.0)
    st = inv.get("status") or "ok"
    if st in ("dead", "fault"):
        return max(0.0, fair - (inv.get("window_kwh") or 0.0))
    if st == "underperforming" and inv.get("peer_index"):
        try:
            pi = max(float(inv["peer_index"]), 0.01)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, fair / pi - (inv.get("window_kwh") or 0.0))
    return 0.0


def _column_recoverable(col: dict, rate: float) -> dict:
    invs = col.get("inverters") or []
    total_np = sum((i.get("nameplate_kw") or 0.0) for i in invs) or 1.0
    fleet_win = sum((i.get("window_kwh") or 0.0) for i in invs)
    per_inv = []
    lost_kwh = 0.0
    for i in invs:
        lk = _est_lost_kwh_window(i, fleet_win, total_np)
        if lk > 0:
            lost_kwh += lk
            per_inv.append({
                "name": i.get("name"),
                "status": i.get("status"),
                "est_lost_kwh_14d": round(lk, 1),
                "est_loss_usd_month": round(lk * rate / _LOSS_WINDOW_DAYS * 30.0, 2),
            })
    return {
        "est_lost_kwh_14d": round(lost_kwh, 1),
        "est_loss_usd_month": round(lost_kwh * rate / _LOSS_WINDOW_DAYS * 30.0, 2),
        "priced_inverters": per_inv,
    }


# Live overlay thresholds — must match public/fleet-store.js liveVerdict
# (Spreadsheet "NEED ATTENTION" counts 14-day health AND live dark/low).
_LIVE_FLOOR_W = 25.0
_LOW_PEER_GAP = 0.15
_LIVE_PEER_MED_MIN = 0.30


def _live_floor_w(inv: dict) -> float:
    np = inv.get("nameplate_kw")
    if np:
        try:
            return max(_LIVE_FLOOR_W, float(np) * 1000.0 * 0.01)
        except (TypeError, ValueError):
            pass
    return _LIVE_FLOOR_W


def _is_producing_now(inv: dict) -> bool:
    p = inv.get("current_power_w")
    return p is not None and float(p) > _live_floor_w(inv)


def _pct_of_max_live(inv: dict) -> float | None:
    np = inv.get("nameplate_kw")
    p = inv.get("current_power_w")
    if p is None or not np:
        return None
    try:
        return float(p) / (float(np) * 1000.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _same_peer_inv(a: dict, b: dict) -> bool:
    if a is b:
        return True
    for key in ("inverter_id", "id", "sn", "serial"):
        av, bv = a.get(key), b.get(key)
        if av is not None and av == bv:
            return True
    return False


def _ui_live_verdict(inv: dict, peers: list[dict], is_daylight: bool | None) -> str:
    """Mirror FleetStore.liveVerdict — what Spreadsheet / cards count as attention.

    Returns: ok | dark | low | stale
    Deliberately separate from 14-day inv.status (peer_analysis).
    """
    if inv.get("no_energy_register"):
        return "ok"
    if is_daylight is False:
        return "ok"
    peers = peers or []
    if _is_producing_now(inv):
        lit = [p for p in peers if not _same_peer_inv(p, inv) and _is_producing_now(p)]
        if len(lit) < 2:
            return "ok"
        my = _pct_of_max_live(inv)
        if my is None:
            return "ok"
        peer_pcts = sorted(
            v for v in (_pct_of_max_live(p) for p in lit) if v is not None
        )
        if len(peer_pcts) < 2:
            return "ok"
        med = peer_pcts[len(peer_pcts) // 2]
        if med < _LIVE_PEER_MED_MIN:
            return "ok"
        if my < med * (1.0 - _LOW_PEER_GAP):
            return "low"
        return "ok"
    lit_n = sum(
        1 for p in peers if not _same_peer_inv(p, inv) and _is_producing_now(p)
    )
    if lit_n >= 2:
        return "dark" if inv.get("current_power_w") is not None else "stale"
    # Solo: fresh hard zero from a proven producer
    if inv.get("current_power_w") is not None:
        daily = inv.get("daily") or []
        try:
            peak = max((float(d.get("kwh") or 0) for d in daily), default=0.0)
        except (TypeError, ValueError):
            peak = 0.0
        np = inv.get("nameplate_kw")
        if np and peak >= float(np) * 4.6 * 0.25:
            return "dark"
    return "ok"


def _column_live_anomalies(col: dict) -> list[dict]:
    """Per-inverter live dark/low flags for one array column (UI parity)."""
    invs = list(col.get("inverters") or [])
    is_day = col.get("is_daylight")
    out: list[dict] = []
    for inv in invs:
        st = inv.get("status") or "ok"
        # Live overlay only applies when 14-day health still says ok (UI rule)
        if st not in ("ok", None):
            continue
        lv = _ui_live_verdict(inv, invs, is_day)
        if lv in ("dark", "low"):
            out.append({
                "name": inv.get("name") or inv.get("sn") or "inverter",
                "sn": inv.get("sn") or inv.get("serial"),
                "status_14d": st,
                "live": lv,
                "current_power_w": inv.get("current_power_w"),
                "nameplate_kw": inv.get("nameplate_kw"),
                "pct_of_max": _pct_of_max_live(inv),
                "diagnosis": inv.get("diagnosis"),
            })
    return out


def _explain_array_attention(col: dict) -> str:
    """Plain-English why this array is flagged (for the agent to speak)."""
    alert = col.get("alert") or {}
    level = alert.get("level") or "ok"
    status = alert.get("status") or "ok"
    headline = alert.get("headline") or ""
    src = col.get("source_status") or {}
    sync = col.get("sync_status") or {}
    bits = []
    if level in ("warn", "critical") or (alert.get("count") or 0) > 0:
        bits.append(headline or f"worst inverter status: {status}")
        n = alert.get("count") or 0
        if n:
            bits.append(f"{n} inverter(s) flagged on 14-day health")
    src_state = (src.get("state") or "").lower()
    if _source_needs_attention(col):
        age = src.get("age_hours")
        age_s = f" (~{age:.0f}h old)" if isinstance(age, (int, float)) else ""
        bits.append(f"source data {src_state}{age_s}")
    elif src_state == "unpolled" and col.get("is_daylight") is not False:
        bits.append(
            "no recent browser capture (SMA/Fronius/Chint only update when the "
            "extension is open and signed in)"
        )
    elif src_state in ("stale", "dark", "offline", "unpolled") and col.get("is_daylight") is False:
        # Overnight quiet — only mention if something ELSE is wrong (14d/live),
        # so we don't invent a "source problem" at night.
        pass
    if sync.get("age_min") is not None and float(sync["age_min"]) > 24 * 60:
        # Multi-day sync gap only; overnight is fine
        if col.get("is_daylight") is not False or float(sync["age_min"]) > 36 * 60:
            bits.append(f"last Array Operator sync ~{float(sync['age_min']) / 60:.0f}h ago")
    if col.get("produced_today_kwh") in (None, 0) and col.get("is_daylight"):
        bits.append("no measured production today while sun is up")
    # Live overlays (what Spreadsheet badges as NEED ATTENTION while 14-day is ok)
    live = _column_live_anomalies(col)
    n_dark = sum(1 for x in live if x.get("live") == "dark")
    n_low = sum(1 for x in live if x.get("live") == "low")
    if n_dark:
        bits.append(f"{n_dark} inverter(s) dark right now while peers produce")
    if n_low:
        bits.append(f"{n_low} inverter(s) low vs peers right now")
    for item in live[:6]:
        label = item.get("name") or "inverter"
        if item.get("live") == "dark":
            bits.append(f"{label}: dark now (live)")
        elif item.get("live") == "low":
            pct = item.get("pct_of_max")
            pct_s = f" (~{pct * 100:.0f}% of nameplate)" if isinstance(pct, (int, float)) else ""
            bits.append(f"{label}: low vs peers now{pct_s}")
    bad = [
        inv for inv in (col.get("inverters") or [])
        if (inv.get("status") or "ok") not in ("ok", "monitoring") or inv.get("no_energy_register")
    ]
    for inv in bad[:6]:
        label = inv.get("name") or inv.get("sn") or "inverter"
        if inv.get("no_energy_register"):
            bits.append(f"{label}: live power but no energy register / history")
        elif inv.get("diagnosis"):
            bits.append(f"{label}: {inv.get('diagnosis')}")
        elif inv.get("status") and inv.get("status") not in ("ok", "monitoring"):
            bits.append(f"{label}: {inv.get('status')}")
    if not bits:
        return "No attention flags on this array right now."
    # de-dupe while preserving order
    seen = set()
    out = []
    for b in bits:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return "; ".join(out)


def _array_needs_attention(col: dict) -> bool:
    """True when Spreadsheet/Triage would show attention — 14-day OR live overlays.

    Night-aware: matches FleetStore._feedBehind — overnight source quiet and
    zero live power are sleep, not faults. Multi-day 14-day health flags still count.
    """
    alert = col.get("alert") or {}
    if (alert.get("level") or "ok") in ("warn", "critical"):
        return True
    if (alert.get("count") or 0) > 0:
        return True
    if _source_needs_attention(col):
        return True
    for inv in col.get("inverters") or []:
        st = inv.get("status") or "ok"
        if st not in ("ok", "monitoring"):
            return True
        if inv.get("no_energy_register"):
            return True
    # Live dark/low (status stays "ok" for up to ~2 days) — matches vendor-sheet
    # (_ui_live_verdict already returns ok when is_daylight is False)
    if _column_live_anomalies(col):
        return True
    # Daylight + no power + no fresh production can also surface as blind/offline
    if col.get("is_daylight") and col.get("current_power_w") is None:
        src = (col.get("source_status") or {}).get("state") or ""
        if src in ("stale", "dark", "offline", "none", "unpolled", ""):
            # Only if we also have no today kWh (Cover Rooftop blind case)
            if col.get("produced_today_kwh") in (None, 0):
                return True
    return False


def _next_step_for_array(col: dict) -> str:
    vendors = [str(v).lower() for v in (col.get("vendors") or []) if v]
    if col.get("vendor"):
        vendors.append(str(col["vendor"]).lower())
    vendors = list(dict.fromkeys(vendors))
    src = (col.get("source_status") or {}).get("state") or ""
    alert_st = (col.get("alert") or {}).get("status") or "ok"
    ext = {"sma", "fronius", "chint"}
    if vendors and set(vendors).issubset(ext) and src in ("unpolled", "stale", "none", ""):
        brand = vendors[0].upper() if vendors else "the vendor"
        return (
            f"Open Inverters → this array → Log in with {brand} so the extension "
            "captures a fresh snapshot; SMA/Fronius/Chint only refresh while a "
            "signed-in browser with the helper is open."
        )
    if alert_st in ("fault", "error", "dead"):
        return (
            "Open the inverter detail / vendor portal from the array card, check "
            "fault codes, open a repair ticket + check in with the assigned O&M contact "
            "(repair_ops_overview), and draft a manufacturer warranty claim if it's dead "
            "with loss evidence."
        )
    if alert_st in ("underperforming", "comm_gap"):
        return (
            "Compare peer index vs siblings on this site; if one unit is lagging, "
            "inspect wiring/shading or open the vendor portal for that serial."
        )
    if src in ("stale", "dark", "offline"):
        return "Vendor source looks offline — check the monitoring portal and site connectivity."
    return "Open #arrays, focus this site, and review the flagged inverters."


def _fleet_tree_columns(db, tenant: Tenant) -> tuple[list[dict], dict]:
    """Shared loader: live fleet-tree columns + summary (stable verdicts = UI/email)."""
    try:
        from . import inverter_fleet
        tree = inverter_fleet.build_fleet_tree(
            db, tenant, force_refresh=False, stable_verdicts=True,
        )
        return list(tree.get("columns") or []), dict(tree.get("summary") or {})
    except Exception as e:
        log.exception("energy_agent fleet tree failed")
        return [], {"error": str(e)}


def _summarize_column(col: dict) -> dict:
    bad = [
        _slim_inverter(inv)
        for inv in (col.get("inverters") or [])
        if (inv.get("status") or "ok") not in ("ok", "monitoring") or inv.get("no_energy_register")
    ]
    live = _column_live_anomalies(col)
    needs = _array_needs_attention(col)
    # Combined attention count matches Spreadsheet "N need attention" spirit
    attn_units = len(bad) + len(live)
    night = col.get("is_daylight") is False
    if needs:
        why = _explain_array_attention(col)
    elif night:
        why = (
            "All clear — sun is down at this site; zero live power overnight is normal sleep, "
            "not an outage."
        )
    else:
        why = "All clear"
    return {
        "id": col.get("array_id"),
        "name": col.get("array_name"),
        "vendor": col.get("vendor"),
        "vendors": col.get("vendors") or ([col["vendor"]] if col.get("vendor") else []),
        "inverter_count": col.get("inverter_count") or len(col.get("inverters") or []),
        "current_power_w": col.get("current_power_w"),
        "produced_today_kwh": col.get("produced_today_kwh"),
        "produced_today_source": col.get("produced_today_source"),
        "is_daylight": col.get("is_daylight"),
        "solar_state": "night" if night else "daylight",
        "alert": col.get("alert"),
        "source_status": col.get("source_status"),
        "sync_status": col.get("sync_status"),
        "needs_attention": needs,
        "why": why,
        "next_step": _next_step_for_array(col) if needs else None,
        "problem_inverters": bad,
        "problem_inverter_count": len(bad),
        # Live-only (14-day still "ok") — critical so EA matches Spreadsheet
        "live_anomalies": live,
        "live_dark_count": sum(1 for x in live if x.get("live") == "dark"),
        "live_low_count": sum(1 for x in live if x.get("live") == "low"),
        "attention_unit_count": attn_units,
    }


def _match_vendor(col: dict, vendor: str | None) -> bool:
    if not vendor:
        return True
    v = vendor.strip().lower()
    if not v:
        return True
    aliases = {
        "se": "solaredge", "solar edge": "solaredge",
        "cps": "chint", "chint/cps": "chint",
    }
    v = aliases.get(v, v)
    vendors = [str(x).lower() for x in (col.get("vendors") or []) if x]
    if col.get("vendor"):
        vendors.append(str(col["vendor"]).lower())
    return any(v == x or v in x or x in v for x in vendors)


def _fleet_overview_tool(db, tenant: Tenant, args: dict) -> dict:
    cols, summary = _fleet_tree_columns(db, tenant)
    if summary.get("error") and not cols:
        return {
            "error": summary["error"],
            "arrays": [],
            "count": 0,
            "hint": "fleet-tree failed; escalate if this keeps happening",
        }
    vendor = (args.get("vendor") or "").strip() or None
    only_attn = bool(args.get("needs_attention_only"))
    arrays = []
    for col in cols:
        if not _match_vendor(col, vendor):
            continue
        row = _summarize_column(col)
        if only_attn and not row["needs_attention"]:
            continue
        arrays.append(row)
    attention = [a for a in arrays if a["needs_attention"]]
    live_units = sum(int(a.get("live_dark_count") or 0) + int(a.get("live_low_count") or 0) for a in arrays)
    health_units = sum(int(a.get("problem_inverter_count") or 0) for a in arrays)
    clock = _fleet_clock_context()
    return {
        "clock": clock,
        "summary": {
            **summary,
            # Override naive tree attention (14-day only) with UI-parity counts
            "attention": len(attention),
            "attention_arrays": len(attention),
            "attention_units_14d": health_units,
            "attention_units_live": live_units,
            "attention_units_total": health_units + live_units,
            "arrays_returned": len(arrays),
            "attention_in_result": len(attention),
            "vendor_filter": vendor,
            "needs_attention_only": only_attn,
            "solar_state": clock.get("solar_state"),
            "fleet_local_now": clock.get("fleet_local_now"),
        },
        "attention_arrays": attention,
        "arrays": arrays,
        "count": len(arrays),
        "note": (
            "Attention = 14-day peer health PLUS live dark/low overlays "
            "(same as Spreadsheet NEED ATTENTION). "
            "Do NOT say a site is healthy if it appears in attention_arrays. "
            "For SMA/Fronius/Chint, source stale often means no recent extension capture. "
            "At night (clock.solar_state=night / is_daylight=false): zero live power is "
            "normal sleep — not an outage."
        ),
        "instruction_for_agent": (
            "When the owner asks how the fleet is doing: report TOTAL attention "
            "arrays and units first, then name EACH attention array with why "
            "(include live_anomalies names). Never summarize only the worst 14-day "
            "underperformer if live overlays flag more units. If clock.solar_state is "
            "night and attention is 0, say the fleet is asleep / looks clear overnight — "
            "do not invent a live-power problem."
        ),
    }


def _investigate_attention_tool(db, tenant: Tenant, args: dict) -> dict:
    cols, summary = _fleet_tree_columns(db, tenant)
    if summary.get("error") and not cols:
        return {"error": summary["error"], "problems": [], "count": 0}
    vendor = (args.get("vendor") or "").strip() or None
    name_q = (args.get("array_name") or args.get("name") or "").strip().lower()
    try:
        limit = int(args.get("limit") or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, 80))

    rate = _tenant_energy_rate(tenant)
    fleet_lost_kwh = 0.0
    problems = []
    for col in cols:
        if not _match_vendor(col, vendor):
            continue
        if name_q and name_q not in str(col.get("array_name") or "").lower():
            continue
        if not _array_needs_attention(col):
            continue
        row = _summarize_column(col)
        # Full inverter list for the investigation (not only bad ones)
        row["all_inverters"] = [
            _slim_inverter(inv) for inv in (col.get("inverters") or [])
        ]
        # Money view — same math as the Fleet Triage "Recoverable" tile
        rec = _column_recoverable(col, rate)
        fleet_lost_kwh += rec["est_lost_kwh_14d"]
        row["recoverable"] = rec
        problems.append(row)

    # Rank: critical first, then by total attention units (14d + live)
    rank = {"critical": 0, "warn": 1, "ok": 2}

    def _key(r):
        lvl = ((r.get("alert") or {}).get("level") or "ok")
        units = int(r.get("attention_unit_count") or 0)
        return (rank.get(lvl, 9), -units, r.get("name") or "")

    problems.sort(key=_key)
    total_found = len(problems)
    problems = problems[:limit]

    # Spoken-ready brief for the model — include live unit names so voice can't skip them
    lines = []
    total_units = 0
    for p in problems:
        units = int(p.get("attention_unit_count") or 0)
        total_units += units
        live_bits = []
        for la in (p.get("live_anomalies") or [])[:8]:
            live_bits.append(f"{la.get('name')}={la.get('live')}")
        extra = f" | live: {', '.join(live_bits)}" if live_bits else ""
        loss_mo = ((p.get("recoverable") or {}).get("est_loss_usd_month") or 0)
        money = f" | ~${loss_mo:,.0f}/mo recoverable" if loss_mo >= 1 else ""
        lines.append(
            f"• {p.get('name')} ({', '.join(p.get('vendors') or []) or 'unknown vendor'}) "
            f"[{units} unit(s)]: {p.get('why')}{extra}{money} → {p.get('next_step')}"
        )
    clock = _fleet_clock_context()
    night = clock.get("solar_state") == "night"
    if lines:
        brief = (
            f"ATTENTION TOTALS: {total_found} array(s), ~{total_units} unit(s) "
            f"(14-day health + live dark/low). List every array below — do not drop sites.\n"
            + "\n".join(lines)
        )
        if night:
            brief = (
                f"FLEET CLOCK: {clock.get('fleet_local_now')} — NIGHT at the sites. "
                "Live power is expected to be ~0; flags below are multi-day health / "
                "multi-day data issues, not 'not producing right now'.\n"
                + brief
            )
    else:
        brief = (
            "No arrays currently need attention"
            + (f" for vendor={vendor}" if vendor else "")
            + (f" matching '{name_q}'" if name_q else "")
            + "."
        )
        if night:
            brief = (
                f"FLEET CLOCK: {clock.get('fleet_local_now')} — NIGHT at the sites. "
                "Zero live power is normal sleep. " + brief
            )

    recoverable_usd_month = round(fleet_lost_kwh * rate / _LOSS_WINDOW_DAYS * 30.0, 2)
    return {
        "clock": clock,
        "count": total_found,
        "returned": len(problems),
        "attention_unit_count": total_units,
        "recoverable_usd_month": recoverable_usd_month,
        "energy_rate_used": rate,
        "recoverable_note": (
            "Recoverable $ mirrors the Fleet Triage tile: confirmed dead/fault/"
            "underperforming only, priced at the tenant's billed $/kWh"
            + ("" if rate != _ENERGY_RATE_FALLBACK else " (fallback rate — no billed rate set)")
            + ". Live dark/low anomalies are unpriced until the peer window confirms them."
        ),
        "fleet_summary": {
            **summary,
            "attention": total_found,
            "attention_units_total": total_units,
            "solar_state": clock.get("solar_state"),
            "fleet_local_now": clock.get("fleet_local_now"),
        },
        "vendor_filter": vendor,
        "problems": problems,
        "brief": brief,
        "instruction_for_agent": (
            "GROUND TRUTH for 'how is my fleet'. Answer SCANNABLE, not a report: (1) one "
            "lead sentence with the TRUE total and the total recoverable_usd_month (never "
            "hide the count); (2) the top 1–3 arrays by $/impact as a short list, one line "
            "each — then 'and N more — want the full list?' rather than pasting all N "
            "(unless the total is ≤3 or they asked for all). Bold at most the headline "
            "number. Do not ask for IDs. If count is 0, say the fleet looks clear in one "
            "line (and if clock.solar_state is night, say they're asleep overnight) and "
            "offer to open Inverters for a double-check. NEVER invent a live-power outage "
            "at night."
        ),
    }


def _production_forecast_tool(db, tenant: Tenant, args: dict) -> dict:
    """Weather-aware predicted-vs-actual — the Analysis 'Production vs expected'
    math, served to the agent. Snapshot-first (same cache the tab uses) so a
    chat turn never fans out to geocode + Open-Meteo when a warm snapshot exists."""
    try:
        wd = int(args.get("window_days") or 14)
    except (TypeError, ValueError):
        wd = 14
    wd = max(3, min(wd, 30))
    from . import array_owners as ao

    data = None
    try:
        snap = db.execute(
            select(ao.FleetForecastSnapshot).where(
                ao.FleetForecastSnapshot.tenant_id == tenant.id,
                ao.FleetForecastSnapshot.window_days == wd,
            )
        ).scalar_one_or_none()
        if snap is not None and (
            (_now() - snap.computed_at).total_seconds() < ao._FLEET_SNAPSHOT_MAX_SERVE_S
        ):
            data = snap.payload
    except Exception as e:
        log.warning("forecast snapshot read failed: %s", e)
    if data is None:
        # Inline compute opens its own short sessions; release ours first so we
        # never hold a pooled connection across the Open-Meteo/geocode HTTP.
        try:
            db.commit()
        except Exception:
            db.rollback()
        data = ao.compute_fleet_forecast(tenant, wd)

    if not data.get("available"):
        return {
            "available": False,
            "window_days": wd,
            "skipped": data.get("skipped") or [],
            "note": (
                "No arrays could be weather-modeled (needs nameplate + a location "
                "or an operator kWh/kW target). Set locations on Analysis to enable this."
            ),
        }

    rows = data.get("rows") or []
    aid = args.get("array_id")
    name_q = (args.get("array_name") or "").strip().lower()
    if aid is not None:
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            aid = None
    if aid is not None:
        rows = [r for r in rows if r.get("array_id") == aid]
    elif name_q:
        rows = [r for r in rows if name_q in str(r.get("name") or "").lower()]

    slim = [
        {
            "array_id": r.get("array_id"),
            "name": r.get("name"),
            "ratio_pct": r.get("ratio_pct"),
            "expected_kwh": r.get("expected_matched_kwh"),
            "actual_kwh": r.get("actual_kwh"),
            "kwh_per_kw_day": r.get("kwh_per_kw_day"),
            "expected_basis": r.get("expected_basis"),
            "measured_days": r.get("measured_days"),
            "confidence": r.get("confidence"),
        }
        for r in rows[:40]
    ]
    return {
        "available": True,
        "window": data.get("window"),
        "fleet": {
            "ratio_pct": data.get("ratio_pct"),
            "expected_kwh": data.get("expected_kwh"),
            "actual_kwh": data.get("actual_kwh"),
            "performance_ratio_measured": data.get("performance_ratio_measured"),
            "confidence": data.get("confidence"),
            "kwh_per_kw": data.get("kwh_per_kw"),
            "arrays_modeled": data.get("arrays_modeled"),
            "arrays_skipped": data.get("arrays_skipped"),
        },
        "arrays": slim,
        "skipped": (data.get("skipped") or [])[:20],
        "sunny_spotlight": data.get("sunny_spotlight"),
        "instruction_for_agent": (
            "ratio_pct is actual÷weather-expected over measured days. Use it to "
            "separate 'cloudy week' (fleet ratio normal, expected low) from a real "
            "site problem (one array's ratio far below the fleet's). Quote confidence. "
            "Never extrapolate across unmeasured days."
        ),
    }


def _list_recent_invoices_tool(db, tenant: Tenant, args: dict) -> dict:
    """Offtaker invoice money view: pending drafts + recently sent, with dollars."""
    from .models import ReportDraft

    try:
        limit = int(args.get("limit") or 12)
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(limit, 30))
    status = (args.get("status") or "all").strip().lower()

    q = select(ReportDraft).where(ReportDraft.tenant_id == tenant.id)
    if status == "pending":
        q = q.where(ReportDraft.status == "pending")
    elif status == "sent":
        q = q.where(ReportDraft.status == "sent")
    drafts = db.execute(
        q.order_by(ReportDraft.created_at.desc()).limit(limit)
    ).scalars().all()

    def _d(x: ReportDraft) -> dict:
        return {
            "draft_id": x.id,
            "offtaker": x.customer_name,
            "status": x.status,
            "period": x.period_label,
            "kwh": x.customer_kwh,
            "amount_usd": x.amount_usd,
            "invoice_number": x.invoice_number,
            "created_at": x.created_at.isoformat() + "Z" if x.created_at else None,
            "sent_at": x.sent_at.isoformat() + "Z" if x.sent_at else None,
        }

    pending_total = db.execute(
        select(func.coalesce(func.sum(ReportDraft.amount_usd), 0.0)).where(
            ReportDraft.tenant_id == tenant.id, ReportDraft.status == "pending",
        )
    ).scalar() or 0.0
    sent_30d_total = db.execute(
        select(func.coalesce(func.sum(ReportDraft.amount_usd), 0.0)).where(
            ReportDraft.tenant_id == tenant.id,
            ReportDraft.status == "sent",
            ReportDraft.sent_at >= _now() - timedelta(days=30),
        )
    ).scalar() or 0.0

    return {
        "invoices": [_d(x) for x in drafts],
        "pending_total_usd": round(float(pending_total), 2),
        "sent_last_30d_total_usd": round(float(sent_30d_total), 2),
        "note": (
            "Amounts are the drafted/sent offtaker invoice dollars (utility-bill × "
            "share math). Approve/send lives on Invoices — you can navigate there "
            "but never send an invoice yourself."
        ),
    }


def _array_detail_tool(db, tenant: Tenant, args: dict) -> dict:
    cols, _summary = _fleet_tree_columns(db, tenant)
    if not cols:
        return {"error": "no fleet columns", "array": None}
    aid = args.get("array_id")
    name_q = (args.get("name") or args.get("array_name") or "").strip().lower()
    match = None
    if aid is not None:
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            return {"error": f"invalid array_id: {aid}"}
        for col in cols:
            if col.get("array_id") == aid:
                match = col
                break
    elif name_q:
        matches = [
            c for c in cols
            if name_q in str(c.get("array_name") or "").lower()
        ]
        if not matches:
            return {
                "error": f"no array matching '{name_q}'",
                "candidates": [
                    {"id": c.get("array_id"), "name": c.get("array_name"), "vendor": c.get("vendor")}
                    for c in cols[:30]
                ],
            }
        if len(matches) > 1:
            exact = [c for c in matches if str(c.get("array_name") or "").lower() == name_q]
            if len(exact) == 1:
                matches = exact
            else:
                return {
                    "error": "multiple arrays match — pass array_id",
                    "matches": [
                        {"id": c.get("array_id"), "name": c.get("array_name"), "vendor": c.get("vendor")}
                        for c in matches[:12]
                    ],
                }
        match = matches[0]
    else:
        return {"error": "pass array_id or name"}

    if match is None:
        return {"error": "array not found", "array": None}
    row = _summarize_column(match)
    row["all_inverters"] = [_slim_inverter(inv) for inv in (match.get("inverters") or [])]
    row["reminder"] = match.get("reminder")
    row["portfolio_name"] = match.get("portfolio_name")
    row["origin_links"] = match.get("origin_links")
    clock = _fleet_clock_context()
    return {
        "clock": clock,
        "array": row,
        "needs_attention": row["needs_attention"],
        "why": row["why"],
        "note": (
            "Read is_daylight / solar_state on the array and clock.solar_state. "
            "At night, zero live power is normal sleep — not an outage."
        ),
    }


# ── Free-mind data plane (tenant-scoped read-only reasoning) ─────────────────
# Authoritative support knowledge lives in energy_agent_support_map.md (## topics).
# Coding/ops agents still use skill solar-operator-energyagent — that skill points
# here for product behavior the in-app agent must explain correctly.

_SUPPORT_MAP_PATH = Path(__file__).with_name("energy_agent_support_map.md")
_SURFACE_MODEL_PATH = Path(__file__).with_name("energy_agent_surface_model.md")
_PRODUCT_MAP_CACHE: dict[str, str] | None = None
_PRODUCT_MAP_MTIME: float | None = None

# Minimal emergency fallback if the markdown file is missing at runtime.
_PRODUCT_MAP_FALLBACK: dict[str, str] = {
    "tabs": (
        "TOP NAV labels: Fleet Triage (#dashboard), Inverters (#arrays), "
        "Analysis (#analysis; trends is a sub-view), Invoices (#reports), "
        "Operations (#ops; Resources is a sub-tab at #resources), Account (#account). "
        "Never say Dashboard/Arrays/Reports/Trends as top tabs; Resources is not a top tab."
    ),
    "system": (
        "Array Operator (arrayoperator.com) = EnergyAgent owner product. "
        "Tenant → Arrays → Inverters; bills → offtaker invoices. "
        "Auto-refresh cloud vs device is password path; API keys are separate."
    ),
    "capture": (
        "Auto-refresh: cloud = store passwords, harvester 24/7; device = extension vault. "
        "PLUS extension one-click Log-in-with capture attaches SMA/Fronius/Chint without a cloud vault row. "
        "SolarEdge usually API keys; never equate fleet vendors with cloud_capture.logins only."
    ),
}


def _parse_support_map_md(text: str) -> dict[str, str]:
    """Split energy_agent_support_map.md into {topic: body} on ## headings."""
    topics: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                topics[current] = "\n".join(buf).strip()
            current = line[3:].strip().lower().split()[0]  # first word = topic id
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current:
        topics[current] = "\n".join(buf).strip()
    return {k: v for k, v in topics.items() if v}


def load_product_map(*, force: bool = False) -> dict[str, str]:
    """Load support topics + surface mental model (mtime-aware cache).

    Support map = domain mechanics. Surface model = macro/meso/micro page atlas
    (why each tab exists, user goals, real controls, nav graph).
    """
    global _PRODUCT_MAP_CACHE, _PRODUCT_MAP_MTIME
    mtimes: list[float] = []
    for p in (_SUPPORT_MAP_PATH, _SURFACE_MODEL_PATH):
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            pass
    mtime = max(mtimes) if mtimes else None
    if (
        _PRODUCT_MAP_CACHE is not None
        and not force
        and mtime is not None
        and mtime == _PRODUCT_MAP_MTIME
    ):
        return _PRODUCT_MAP_CACHE
    try:
        raw = _SUPPORT_MAP_PATH.read_text(encoding="utf-8")
        parsed = _parse_support_map_md(raw)
        if not parsed:
            raise ValueError("no ## topics in support map")
        # Merge surface atlas topics (product_spine, surface_invoices, …)
        try:
            surf = _SURFACE_MODEL_PATH.read_text(encoding="utf-8")
            for k, v in _parse_support_map_md(surf).items():
                parsed[k] = v
            # Convenience alias: product_map(topic=surface) → full spine + playbook
            if "product_spine" in parsed:
                bits = [parsed["product_spine"]]
                for key in (
                    "orientation_playbook",
                    "anti_hallucination",
                    "surface_global",
                ):
                    if key in parsed:
                        bits.append(parsed[key])
                parsed["surface"] = "\n\n".join(bits)
        except OSError as se:
            log.warning("surface model missing (%s)", se)
        _PRODUCT_MAP_CACHE = parsed
        _PRODUCT_MAP_MTIME = mtime
        return parsed
    except Exception as exc:
        log.warning("energy_agent support map load failed (%s) — using fallback", exc)
        _PRODUCT_MAP_CACHE = dict(_PRODUCT_MAP_FALLBACK)
        _PRODUCT_MAP_MTIME = mtime
        return _PRODUCT_MAP_CACHE


# Eager load so import surfaces a missing map early (falls back if needed).
PRODUCT_MAP = load_product_map()


def _tenant_census_tool(db, tenant: Tenant, args: dict) -> dict:
    """Ground-truth inventory from ORM — not filtered fleet-tree."""
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload
    from .models import (
        BillingReportSubscription, DailyGeneration, Inverter, InverterConnection,
        UtilityAccount,
    )

    tid = tenant.id
    include_names = args.get("include_names", True)
    if include_names is None:
        include_names = True

    arrays = db.execute(
        select(Array).options(selectinload(Array.client)).where(
            Array.tenant_id == tid, Array.deleted_at.is_(None),
        ).order_by(Array.id)
    ).scalars().all()
    array_ids = [a.id for a in arrays]

    invs = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tid, Inverter.deleted_at.is_(None),
        ).order_by(Inverter.array_id, Inverter.position, Inverter.id)
    ).scalars().all() if True else []

    conns = db.execute(
        select(InverterConnection).where(
            InverterConnection.tenant_id == tid,
        )
    ).scalars().all() if hasattr(InverterConnection, "tenant_id") else []
    # Some schemas key connections by array only
    if not conns and array_ids:
        try:
            conns = db.execute(
                select(InverterConnection).where(
                    InverterConnection.array_id.in_(array_ids),
                )
            ).scalars().all()
        except Exception:
            conns = []

    util = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all() if hasattr(UtilityAccount, "deleted_at") else db.execute(
        select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
    ).scalars().all()

    offtaker_q = select(BillingReportSubscription).where(
        BillingReportSubscription.tenant_id == tid,
    )
    if hasattr(BillingReportSubscription, "deleted_at"):
        offtaker_q = offtaker_q.where(BillingReportSubscription.deleted_at.is_(None))
    offtakers = db.execute(offtaker_q).scalars().all()

    # Recent production (7d)
    since = (_now().date() - timedelta(days=7))
    recent_kwh = 0.0
    if array_ids:
        recent_kwh = float(db.execute(
            select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0)).where(
                DailyGeneration.array_id.in_(array_ids),
                DailyGeneration.day >= since,
            )
        ).scalar() or 0.0)

    # Per-array inverter counts + vendor mix
    inv_by_array: dict[int, list] = {}
    vendor_counts: dict[str, int] = {}
    for iv in invs:
        inv_by_array.setdefault(iv.array_id, []).append(iv)
        v = (iv.vendor or "unknown").lower()
        vendor_counts[v] = vendor_counts.get(v, 0) + 1

    conn_by_array: dict[int, list] = {}
    for c in conns:
        conn_by_array.setdefault(c.array_id, []).append(c)

    util_array_ids = {u.array_id for u in util if getattr(u, "array_id", None)}

    array_rows = []
    for a in arrays:
        ivs_a = inv_by_array.get(a.id, [])
        conns_a = conn_by_array.get(a.id, [])
        vendors = sorted({(iv.vendor or "").lower() for iv in ivs_a if iv.vendor})
        if not vendors:
            vendors = sorted({(c.vendor or "").lower() for c in conns_a if getattr(c, "vendor", None)})
        if not vendors and getattr(a, "solaredge_site_id", None):
            vendors = ["solaredge"]
        kind = "inverter" if ivs_a or conns_a or getattr(a, "solaredge_site_id", None) else (
            "meter_only" if a.id in util_array_ids else "empty"
        )
        row = {
            "id": a.id,
            "name": a.name,
            "client": a.client.name if a.client else None,
            "nameplate_kw": getattr(a, "nameplate_kw", None) or getattr(a, "capacity_kw", None),
            "vendors": vendors,
            "inverter_count": len(ivs_a),
            "connection_count": len(conns_a),
            "has_utility_meter": a.id in util_array_ids,
            "kind": kind,
            "excluded": bool(getattr(a, "excluded", False)),
            "solaredge_site_id": getattr(a, "solaredge_site_id", None),
        }
        array_rows.append(row)

    inv_rows = []
    if include_names:
        for iv in invs[:400]:
            inv_rows.append({
                "id": iv.id,
                "array_id": iv.array_id,
                "name": iv.name or iv.serial,
                "serial": iv.serial,
                "vendor": iv.vendor,
                "model": iv.model,
                "nameplate_kw": getattr(iv, "nameplate_kw", None),
                "last_seen_at": (
                    iv.last_seen_at.isoformat() + "Z"
                    if getattr(iv, "last_seen_at", None) else None
                ),
            })

    offtaker_rows = []
    if include_names:
        for s in offtakers[:300]:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            offtaker_rows.append({
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "array_id": getattr(s, "array_id", None),
                "utility_account_id": getattr(s, "utility_account_id", None),
                "share_pct": share,
                "enabled": getattr(s, "enabled", None),
            })

    kind_counts = {"inverter": 0, "meter_only": 0, "empty": 0}
    for r in array_rows:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1

    return {
        "tenant_id": tid,
        "company": getattr(tenant, "company_name", None) or getattr(tenant, "name", None),
        "email": getattr(tenant, "contact_email", None),
        "operator_name": getattr(tenant, "operator_name", None),
        "counts": {
            "arrays": len(array_rows),
            "arrays_inverter_backed": kind_counts.get("inverter", 0),
            "arrays_meter_only": kind_counts.get("meter_only", 0),
            "arrays_empty": kind_counts.get("empty", 0),
            "inverters": len(invs),
            "inverter_connections": len(conns),
            "utility_accounts": len(util),
            "offtakers": len(offtakers),
            "offtakers_enabled": sum(1 for s in offtakers if getattr(s, "enabled", True)),
        },
        "inverters_by_vendor": vendor_counts,
        "production_last_7d_kwh": round(recent_kwh, 1),
        "arrays": array_rows if include_names else None,
        "inverters": inv_rows if include_names else None,
        "offtakers": offtaker_rows if include_names else None,
        "notes": [
            "This is database ground truth for THIS tenant only.",
            "fleet_overview health tree may list fewer arrays (skips pure meter-only).",
            "If the UI shows more than this census, session may be a different tenant — check account_summary.",
        ],
    }


def _query_tenant_tool(db, tenant: Tenant, args: dict) -> dict:
    """Structured read-only investigation across allowlisted resources."""
    from sqlalchemy import func
    from .models import (
        BillingReportSubscription, DailyGeneration, Inverter, InverterConnection,
        UtilityAccount,
    )

    tid = tenant.id
    resource = (args.get("resource") or "").strip().lower()
    vendor = (args.get("vendor") or "").strip().lower() or None
    array_id = args.get("array_id")
    array_name = (args.get("array_name") or "").strip().lower() or None
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 300))
    try:
        days = int(args.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 90))
    group_by = (args.get("group_by") or "none").strip().lower()
    question = (args.get("question") or "").strip()

    # Resolve array_name → id if needed
    if array_name and array_id is None:
        for a in db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all():
            if array_name in (a.name or "").lower():
                array_id = a.id
                break

    if resource == "arrays":
        rows = []
        for a in db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
            .order_by(Array.id)
        ).scalars().all():
            if array_id is not None and a.id != int(array_id):
                continue
            if array_name and array_name not in (a.name or "").lower():
                continue
            se = bool(getattr(a, "solaredge_site_id", None))
            if vendor == "solaredge" and not se:
                # still include if has SE inverters — checked below cheaper path
                pass
            rows.append({
                "id": a.id,
                "name": a.name,
                "nameplate_kw": getattr(a, "nameplate_kw", None) or getattr(a, "capacity_kw", None),
                "solaredge_site_id": getattr(a, "solaredge_site_id", None),
                "portfolio_name": getattr(a, "portfolio_name", None),
                "excluded": bool(getattr(a, "excluded", False)),
            })
        # Optional vendor filter via inverter presence
        if vendor:
            invs = db.execute(
                select(Inverter.array_id).where(
                    Inverter.tenant_id == tid,
                    Inverter.deleted_at.is_(None),
                    Inverter.vendor.ilike(f"%{vendor}%"),
                ).distinct()
            ).scalars().all()
            allow = set(invs)
            if vendor in ("solaredge", "se"):
                allow |= {r["id"] for r in rows if r.get("solaredge_site_id")}
            rows = [r for r in rows if r["id"] in allow]
        return {
            "resource": "arrays",
            "question": question or None,
            "count": len(rows),
            "rows": rows[:limit],
        }

    if resource == "inverters":
        q = select(Inverter).where(
            Inverter.tenant_id == tid, Inverter.deleted_at.is_(None),
        )
        if array_id is not None:
            q = q.where(Inverter.array_id == int(array_id))
        if vendor:
            q = q.where(Inverter.vendor.ilike(f"%{vendor}%"))
        q = q.order_by(Inverter.array_id, Inverter.position).limit(limit)
        invs = db.execute(q).scalars().all()
        # names of arrays for readability
        arr_names = {
            a.id: a.name for a in db.execute(
                select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
            ).scalars().all()
        }
        rows = [{
            "id": iv.id,
            "array_id": iv.array_id,
            "array_name": arr_names.get(iv.array_id),
            "name": iv.name or iv.serial,
            "serial": iv.serial,
            "vendor": iv.vendor,
            "model": iv.model,
            "nameplate_kw": getattr(iv, "nameplate_kw", None),
            "last_seen_at": (
                iv.last_seen_at.isoformat() + "Z"
                if getattr(iv, "last_seen_at", None) else None
            ),
        } for iv in invs]
        if group_by == "vendor":
            g: dict[str, int] = {}
            for r in rows:
                v = (r.get("vendor") or "unknown").lower()
                g[v] = g.get(v, 0) + 1
            return {"resource": "inverters", "group_by": "vendor", "counts": g, "sample": rows[:20]}
        if group_by == "array":
            g = {}
            for r in rows:
                k = f"{r.get('array_id')}:{r.get('array_name')}"
                g[k] = g.get(k, 0) + 1
            return {"resource": "inverters", "group_by": "array", "counts": g, "sample": rows[:20]}
        return {"resource": "inverters", "question": question or None, "count": len(rows), "rows": rows}

    if resource == "offtakers":
        q = select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid,
        )
        if hasattr(BillingReportSubscription, "deleted_at"):
            q = q.where(BillingReportSubscription.deleted_at.is_(None))
        if array_id is not None:
            q = q.where(BillingReportSubscription.array_id == int(array_id))
        subs = db.execute(q.order_by(BillingReportSubscription.id).limit(limit)).scalars().all()
        pricing_ctx = None
        try:
            from .billing.delivery import build_pricing_ctx
            pricing_ctx = build_pricing_ctx(db, tenant)
        except Exception:
            pass
        rows = []
        for s in subs:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            row = {
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "array_id": getattr(s, "array_id", None),
                "utility_account_id": getattr(s, "utility_account_id", None),
                "share_pct": share,
                "enabled": getattr(s, "enabled", None),
                "delivery_mode": getattr(s, "delivery_mode", None),
            }
            row.update(_offtaker_rate_fields(db, tenant, s, pricing_ctx=pricing_ctx))
            rows.append(row)
        return {"resource": "offtakers", "count": len(rows), "rows": rows}

    if resource == "daily_generation":
        arrs = db.execute(
            select(Array).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
        ).scalars().all()
        arr_ids = [a.id for a in arrs]
        if array_id is not None:
            arr_ids = [int(array_id)] if int(array_id) in arr_ids else []
        if not arr_ids:
            return {"resource": "daily_generation", "count": 0, "rows": [], "total_kwh": 0}
        since = (_now().date() - timedelta(days=days))
        name_by_id = {a.id: a.name for a in arrs}
        if group_by == "array":
            rows = []
            for aid, kwh in db.execute(
                select(DailyGeneration.array_id, func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .where(DailyGeneration.array_id.in_(arr_ids), DailyGeneration.day >= since)
                .group_by(DailyGeneration.array_id)
            ).all():
                rows.append({
                    "array_id": aid,
                    "array_name": name_by_id.get(aid),
                    "kwh": round(float(kwh or 0), 1),
                    "days": days,
                })
            rows.sort(key=lambda r: -r["kwh"])
            return {
                "resource": "daily_generation",
                "group_by": "array",
                "days": days,
                "total_kwh": round(sum(r["kwh"] for r in rows), 1),
                "rows": rows[:limit],
            }
        # day-level series (fleet total)
        day_rows = []
        for day, kwh in db.execute(
            select(DailyGeneration.day, func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
            .where(DailyGeneration.array_id.in_(arr_ids), DailyGeneration.day >= since)
            .group_by(DailyGeneration.day)
            .order_by(DailyGeneration.day.desc())
            .limit(limit)
        ).all():
            day_rows.append({"day": day.isoformat() if hasattr(day, "isoformat") else str(day),
                             "kwh": round(float(kwh or 0), 1)})
        return {
            "resource": "daily_generation",
            "days": days,
            "total_kwh": round(sum(r["kwh"] for r in day_rows), 1),
            "rows": day_rows,
        }

    if resource == "utility_accounts":
        q = select(UtilityAccount).where(UtilityAccount.tenant_id == tid)
        if hasattr(UtilityAccount, "deleted_at"):
            q = q.where(UtilityAccount.deleted_at.is_(None))
        accts = db.execute(q.limit(limit)).scalars().all()
        rows = [{
            "id": u.id,
            "provider": getattr(u, "provider", None),
            "account_number": getattr(u, "account_number", None) or getattr(u, "acct_number", None),
            "nickname": getattr(u, "nickname", None),
            "array_id": getattr(u, "array_id", None),
            "service_address": getattr(u, "service_address", None),
            "label": (
                (getattr(u, "nickname", None) or "").strip()
                or f"{getattr(u, 'provider', '')} {getattr(u, 'account_number', None) or ''}".strip()
            ),
        } for u in accts]
        return {"resource": "utility_accounts", "count": len(rows), "rows": rows}

    if resource == "inverter_connections":
        try:
            q = select(InverterConnection)
            if hasattr(InverterConnection, "tenant_id"):
                q = q.where(InverterConnection.tenant_id == tid)
            else:
                arr_ids = [a.id for a in db.execute(
                    select(Array.id).where(Array.tenant_id == tid, Array.deleted_at.is_(None))
                ).scalars().all()]
                q = q.where(InverterConnection.array_id.in_(arr_ids or [-1]))
            if array_id is not None:
                q = q.where(InverterConnection.array_id == int(array_id))
            if vendor:
                q = q.where(InverterConnection.vendor.ilike(f"%{vendor}%"))
            conns = db.execute(q.limit(limit)).scalars().all()
        except Exception as e:
            return {"resource": "inverter_connections", "error": str(e), "rows": []}
        rows = [{
            "id": c.id,
            "array_id": c.array_id,
            "vendor": getattr(c, "vendor", None),
            "status": getattr(c, "status", None),
            "site_id": (getattr(c, "config", None) or {}).get("site_id")
            if isinstance(getattr(c, "config", None), dict) else None,
        } for c in conns]
        return {"resource": "inverter_connections", "count": len(rows), "rows": rows}

    if resource == "bills_summary":
        # Lightweight: count utility accounts + offtakers + 30d generation
        census = _tenant_census_tool(db, tenant, {"include_names": False})
        gen = _query_tenant_tool(db, tenant, {
            "resource": "daily_generation", "days": 30, "group_by": "array", "limit": 50,
        })
        return {
            "resource": "bills_summary",
            "counts": census.get("counts"),
            "production_last_30d_by_array": gen.get("rows"),
            "production_last_30d_total_kwh": gen.get("total_kwh"),
            "question": question or None,
        }

    if resource == "tenant_pricing":
        return {
            "resource": "tenant_pricing",
            "question": question or None,
            **_tenant_global_rates(tenant),
        }

    if resource == "bills":
        from .models import Bill
        q = select(Bill).where(Bill.tenant_id == tid)
        if array_id is not None and hasattr(Bill, "array_id"):
            q = q.where(Bill.array_id == int(array_id))
        # Newest first
        if hasattr(Bill, "period_end"):
            try:
                q = q.order_by(Bill.period_end.desc().nulls_last(), Bill.id.desc())
            except Exception:
                q = q.order_by(Bill.id.desc())
        else:
            q = q.order_by(Bill.id.desc())
        bills = db.execute(q.limit(limit)).scalars().all()
        rows = []
        for b in bills:
            avg_cents = getattr(b, "avg_rate_cents_kwh", None)
            solar_usd = getattr(b, "solar_credit_usd", None) or getattr(b, "net_credit", None)
            excess = getattr(b, "kwh_sent_to_grid", None)
            implied = None
            try:
                if solar_usd is not None and excess and float(excess) > 0:
                    implied = round(float(solar_usd) / float(excess), 6)
            except Exception:
                pass
            rows.append({
                "id": getattr(b, "id", None),
                "utility_account_id": getattr(b, "utility_account_id", None) or getattr(b, "account_id", None),
                "array_id": getattr(b, "array_id", None),
                "period_start": (
                    b.period_start.isoformat() if getattr(b, "period_start", None) else None
                ),
                "period_end": (
                    b.period_end.isoformat() if getattr(b, "period_end", None) else None
                ),
                "kwh_generated": getattr(b, "kwh_generated", None),
                "kwh_sent_to_grid": excess,
                "solar_credit_usd": solar_usd,
                "implied_solar_credit_rate_per_kwh": implied,
                "avg_rate_cents_kwh": avg_cents,
                "avg_rate_usd_per_kwh": (
                    round(float(avg_cents) / 100.0, 6) if avg_cents is not None else None
                ),
                "total_cost": getattr(b, "total_cost", None),
                "provider": getattr(b, "provider", None) or getattr(b, "supplier", None),
            })
        return {
            "resource": "bills",
            "count": len(rows),
            "rows": rows,
            "question": question or None,
            "note": (
                "implied_solar_credit_rate_per_kwh = solar_credit_usd / excess kWh when both present; "
                "offtaker invoice rates also via get_billing_rates / list_offtakers."
            ),
        }

    return {
        "error": f"unknown resource '{resource}'",
        "allowed": [
            "arrays", "inverters", "offtakers", "daily_generation",
            "utility_accounts", "inverter_connections", "bills_summary",
            "bills", "tenant_pricing",
        ],
    }


def _tenant_global_rates(tenant: Tenant) -> dict:
    net = getattr(tenant, "default_net_rate_per_kwh", None)
    disc = getattr(tenant, "default_discount_pct", None)
    flat = getattr(tenant, "default_billing_rate_per_kwh", None)
    try:
        from .billing.delivery import DEFAULT_DISCOUNT
        default_disc = DEFAULT_DISCOUNT
    except Exception:
        default_disc = 0.10
    if net is not None and float(net) > 0:
        note = (
            f"Master net/solar credit rate is set at ${float(net):.5f}/kWh — "
            "offtakers without a custom override all use it (minus discount)."
        )
        src = "global"
    else:
        note = (
            "Master rate is blank — each offtaker uses solar credit from their "
            "own bound utility bill (or schedule), not a single fleet number."
        )
        src = "per_offtaker_bill"
    return {
        "default_net_rate_per_kwh": net,
        "default_discount_pct": disc,
        "default_billing_rate_per_kwh": flat,
        "effective_discount_pct": disc if disc is not None else default_disc,
        "master_rate_source": src,
        "note": note,
    }


def _offtaker_rate_fields(db, tenant: Tenant, sub, pricing_ctx=None) -> dict:
    """Per-offtaker stored rates + resolved invoice pricing (what bills use).

    Pass `pricing_ctx` from build_pricing_ctx when listing many offtakers —
    without it each resolve opens a new DB session (N+1, multi-second lag).
    """
    out = {
        "rate_per_kwh": getattr(sub, "rate_per_kwh", None),
        "net_rate_per_kwh": getattr(sub, "net_rate_per_kwh", None),
        "discount_pct": getattr(sub, "discount_pct", None),
        "solar_credit_rate_usd_per_kwh": None,
        "solar_credit_source": None,
    }
    try:
        from .billing.delivery import resolve_discount_pricing
        p = resolve_discount_pricing(sub, ctx=pricing_ctx)
        out["resolved_net_rate"] = round(float(p["net_rate"]), 6)
        out["resolved_discount_pct"] = round(float(p["discount_pct"]), 6)
        out["resolved_effective_rate"] = p.get("effective_rate")
        out["resolved_net_source"] = p.get("net_source")
        out["resolved_net_note"] = p.get("net_rate_note")
        # User-facing alias
        out["solar_credit_rate_usd_per_kwh"] = out["resolved_net_rate"]
        out["solar_credit_source"] = p.get("net_source")
    except Exception as e:
        out["pricing_resolve_error"] = str(e)[:240]
    return out


def _validate_ea_rate(rate) -> float | None:
    if rate is None:
        return None
    try:
        r = float(rate)
    except (TypeError, ValueError):
        raise ValueError("rate must be a number ($/kWh)")
    if r < 0 or r > 5.0:
        raise ValueError("rate must be between 0 and 5.0 $/kWh")
    return r


def _validate_ea_discount(pct) -> float | None:
    """Accept 0.10 or 10 for 10%."""
    if pct is None:
        return None
    try:
        d = float(pct)
    except (TypeError, ValueError):
        raise ValueError("discount must be a number")
    if d >= 1.0:
        d = d / 100.0
    if not (0 <= d < 1):
        raise ValueError("discount must be in [0, 1) as fraction or [0, 100) as percent")
    return d


# ── Public web tools (Energy Agent internet access) ─────────────────────────
_WEB_UA = (
    "Mozilla/5.0 (compatible; EnergyAgent/1.0; +https://arrayoperator.com; research)"
)
_BLOCKED_HOST_SUFFIXES = (
    "railway.internal",
    "localhost",
    "local",
    "internal",
    "svc.cluster.local",
)
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "metadata.google.internal",
    "169.254.169.254",
})


def _web_url_allowed(url: str) -> tuple[bool, str]:
    """Reject private/internal targets. Returns (ok, reason)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return False, "invalid URL"
    if p.scheme not in ("http", "https"):
        return False, "only http/https URLs are allowed"
    host = (p.hostname or "").lower().strip(".")
    if not host:
        return False, "missing host"
    if host in _BLOCKED_HOSTS:
        return False, "blocked host"
    if any(host == s or host.endswith("." + s) for s in _BLOCKED_HOST_SUFFIXES):
        return False, "internal host blocked"
    # Private IP ranges (basic)
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.|127\.)", host):
        return False, "private IP blocked"
    return True, "ok"


def _html_to_text(html: str, max_chars: int = 12000) -> str:
    """Very small HTML → text (no extra deps)."""
    if not html:
        return ""
    # Drop scripts/styles
    t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|article)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = (
        t.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:max_chars]


def _web_search_tool(args: dict) -> dict:
    """Live public search via DuckDuckGo (no API key)."""
    import html as html_lib
    from urllib.parse import quote_plus, unquote

    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required", "results": []}
    if len(query) > 300:
        return {"error": "query too long (max 300 chars)", "results": []}
    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(8, max_results))

    results: list[dict] = []
    abstract = None
    abstract_url = None
    abstract_source = None
    related: list[str] = []

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed on server", "results": []}

    headers = {"User-Agent": _WEB_UA, "Accept": "application/json,text/html"}

    # 1) Instant Answer API — good for facts / Wikipedia-style abstracts
    try:
        ia_url = (
            "https://api.duckduckgo.com/?"
            f"q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        )
        with httpx.Client(timeout=httpx.Timeout(12.0), follow_redirects=True) as client:
            r = client.get(ia_url, headers=headers)
            if r.status_code == 200:
                data = r.json() or {}
                abstract = (data.get("AbstractText") or "").strip() or None
                abstract_url = (data.get("AbstractURL") or "").strip() or None
                abstract_source = (data.get("AbstractSource") or "").strip() or None
                for topic in (data.get("RelatedTopics") or [])[:6]:
                    if isinstance(topic, dict):
                        if topic.get("Text"):
                            related.append(str(topic["Text"])[:240])
                        for t2 in topic.get("Topics") or []:
                            if isinstance(t2, dict) and t2.get("Text"):
                                related.append(str(t2["Text"])[:240])
                    if len(related) >= 6:
                        break
                # Official DDG "Results" (often empty)
                for item in data.get("Results") or []:
                    if not isinstance(item, dict):
                        continue
                    u = (item.get("FirstURL") or item.get("url") or "").strip()
                    t = (item.get("Text") or item.get("title") or "").strip()
                    if u and t:
                        results.append({
                            "title": t[:200],
                            "url": u,
                            "snippet": t[:280],
                            "source": "duckduckgo_ia",
                        })
    except Exception as e:
        log.warning("web_search instant-answer failed: %s", e)

    # 2) HTML search — organic links when IA is thin
    if len(results) < max_results:
        try:
            html_url = "https://html.duckduckgo.com/html/"
            with httpx.Client(timeout=httpx.Timeout(14.0), follow_redirects=True) as client:
                r = client.post(
                    html_url,
                    data={"q": query, "b": ""},
                    headers={
                        **headers,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "text/html",
                    },
                )
                body = r.text or ""
            # Result blocks: <a class="result__a" href="...">title</a>
            # DDG wraps redirects: //duckduckgo.com/l/?uddg=<urlencoded>
            link_re = re.compile(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                re.I | re.S,
            )
            snip_re = re.compile(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>',
                re.I | re.S,
            )
            titles = link_re.findall(body)
            snips = snip_re.findall(body)
            seen = {x.get("url") for x in results}
            for idx, (href, title_html) in enumerate(titles):
                if len(results) >= max_results:
                    break
                href = html_lib.unescape(href.strip())
                title = re.sub(r"<[^>]+>", "", html_lib.unescape(title_html)).strip()
                # Unwrap DDG redirect
                m = re.search(r"[?&]uddg=([^&]+)", href)
                if m:
                    href = unquote(m.group(1))
                if href.startswith("//"):
                    href = "https:" + href
                ok, _ = _web_url_allowed(href)
                if not ok or not title or href in seen:
                    continue
                snippet = ""
                if idx < len(snips):
                    snippet = re.sub(
                        r"<[^>]+>", "", html_lib.unescape(snips[idx])
                    ).strip()[:280]
                seen.add(href)
                results.append({
                    "title": title[:200],
                    "url": href,
                    "snippet": snippet,
                    "source": "duckduckgo_html",
                })
        except Exception as e:
            log.warning("web_search html failed: %s", e)

    if not results and not abstract:
        return {
            "query": query,
            "results": [],
            "error": "No web results returned — try a simpler query.",
            "provider": "duckduckgo",
        }

    return {
        "query": query,
        "results": results[:max_results],
        "abstract": abstract,
        "abstract_url": abstract_url,
        "abstract_source": abstract_source,
        "related": related[:6],
        "provider": "duckduckgo",
        "note": (
            "Public web results. Cite title + URL when answering. "
            "For THIS tenant's fleet/offtakers/kWh use census/query tools instead."
        ),
    }


def _web_fetch_tool(args: dict) -> dict:
    """Fetch a public page and return extracted text."""
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "url is required"}
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    ok, reason = _web_url_allowed(url)
    if not ok:
        return {"error": f"URL not allowed: {reason}", "url": url}
    try:
        max_chars = int(args.get("max_chars") or 12000)
    except (TypeError, ValueError):
        max_chars = 12000
    max_chars = max(1000, min(40000, max_chars))

    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed on server", "url": url}

    try:
        with httpx.Client(
            timeout=httpx.Timeout(18.0),
            follow_redirects=True,
            headers={"User-Agent": _WEB_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        ) as client:
            r = client.get(url)
            final = str(r.url)
            ok2, reason2 = _web_url_allowed(final)
            if not ok2:
                return {"error": f"redirect target blocked: {reason2}", "url": url, "final_url": final}
            ct = (r.headers.get("content-type") or "").lower()
            raw = r.content[:800_000]  # hard cap bytes
            if r.status_code >= 400:
                return {
                    "error": f"HTTP {r.status_code}",
                    "url": url,
                    "final_url": final,
                }
            # PDF / binary: don't dump
            if "pdf" in ct or raw[:4] == b"%PDF":
                return {
                    "url": url,
                    "final_url": final,
                    "content_type": ct,
                    "note": "PDF binary — cannot extract full text here. Use web_search or ask the user for key excerpts.",
                    "text": "",
                    "bytes": len(raw),
                }
            try:
                text_body = raw.decode(r.encoding or "utf-8", errors="replace")
            except Exception:
                text_body = raw.decode("utf-8", errors="replace")
            if "html" in ct or text_body.lstrip().lower().startswith("<!doctype") or "<html" in text_body[:200].lower():
                extracted = _html_to_text(text_body, max_chars=max_chars)
            else:
                extracted = text_body[:max_chars]
            title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text_body)
            title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else None
            return {
                "url": url,
                "final_url": final,
                "status": r.status_code,
                "content_type": ct,
                "title": (title or "")[:200] or None,
                "text": extracted,
                "truncated": len(extracted) >= max_chars,
                "note": "Extracted public page text. Quote carefully; prefer primary sources.",
            }
    except Exception as e:
        return {"error": f"fetch failed: {e}", "url": url}


def _product_map_tool(args: dict) -> dict:
    # Reload if the support map file changed (deploy without process restart rare,
    # but local/dev edits should pick up without full restart when force-path used).
    pmap = load_product_map()
    topic = (args.get("topic") or "all").strip().lower()
    if topic in pmap:
        src = (
            "energy_agent_surface_model.md"
            if topic.startswith("surface")
            or topic in (
                "product_spine",
                "orientation_playbook",
                "anti_hallucination",
            )
            else "energy_agent_support_map.md"
        )
        return {
            "topic": topic,
            "map": pmap[topic],
            "source": src,
        }
    # Unknown/all → topic directory + entry-point sections (NOT a dump of every
    # topic; the map now spans many topics — call a specific one for depth).
    keys = sorted(pmap.keys())
    entry = {
        k: pmap[k]
        for k in ("system", "tabs", "surface", "tools")
        if k in pmap
    }
    result = {
        "topic": "directory" if topic == "all" else "unknown",
        "topics": keys,
        "map": "\n\n".join(f"## {k}\n{v}" for k, v in entry.items()),
        "source": "energy_agent_support_map.md",
        "note": (
            "This is the topic directory + entry sections. Call "
            "product_map(topic=<id>) for the full text of any topic above "
            "(e.g. capture, health, offtakers, plans, agent, datamodel)."
        ),
        "tools_to_use": {
            "inventory": "tenant_census",
            "ad_hoc_lists": "query_tenant",
            "health": "investigate_attention | fleet_overview | array_detail",
            "account": "account_summary (contact_email, company, plan, capture_mode, cloud_capture)",
            "how_system_works": "product_map(topic=system|capture) — required before explaining Auto-refresh",
            "peer_vs_portal": "product_map(topic=status)",
            "offtaker_edit": "patch_offtaker (confirm)",
            "nav": "ui_navigate",
            "internet": "web_search | web_fetch — live public web (cite URLs); not for this tenant's kWh",
        },
        "caveat": (
            "You reason over THIS tenant's data + product map + optional public web. "
            "You do not have arbitrary codebase shell access (that would leak other "
            "tenants / secrets)."
        ),
    }
    if topic not in ("all", "", "directory"):
        result["requested_topic_not_found"] = topic
    return result


def _account_summary_tool(db, tenant: Tenant, args: dict) -> dict:
    """Same fields the Account tab shows — never use tenant.email (it's contact_email)."""
    from sqlalchemy import func
    from .models import UtilityAccount, UtilitySession, Bill, Client

    # Fresh row inside this session (caller's tenant may be detached/stale)
    t = db.get(Tenant, tenant.id) or tenant
    include_billing = args.get("include_billing", True)
    if include_billing is None:
        include_billing = True

    accounts_count = 0
    bills_count = 0
    clients_count = 0
    connected_providers: list[str] = []
    last_sess = None
    try:
        accounts_count = int(db.execute(
            select(func.count()).select_from(UtilityAccount)
            .where(UtilityAccount.tenant_id == t.id)
        ).scalar() or 0)
        connected_providers = [
            row[0]
            for row in db.execute(
                select(UtilityAccount.provider)
                .where(UtilityAccount.tenant_id == t.id)
                .distinct()
            ).all()
            if row[0]
        ]
        bills_count = int(db.execute(
            select(func.count()).select_from(Bill).where(Bill.tenant_id == t.id)
        ).scalar() or 0)
        clients_count = int(db.execute(
            select(func.count()).select_from(Client).where(
                Client.tenant_id == t.id, Client.deleted_at.is_(None),
            )
        ).scalar() or 0)
        last_sess = db.execute(
            select(UtilitySession).where(UtilitySession.tenant_id == t.id)
            .order_by(UtilitySession.captured_at.desc())
        ).scalars().first()
    except Exception as e:
        log.warning("account_summary counts: %s", e)

    plan_features = None
    try:
        from .stripe_helpers import ao_plan_features
        plan_features = ao_plan_features(
            getattr(t, "product", None), getattr(t, "billing_plan", None),
        )
    except Exception:
        plan_features = None

    has_pm = bool(getattr(t, "stripe_payment_method_id", None))
    card_brief = {"has_payment_method": has_pm, "card_brand": None, "card_last4": None}
    # Best-effort card brand/last4 via account helpers (never fail the tool)
    try:
        from .account import _resolve_pm_id, _card_brief
        has_pm = _resolve_pm_id(t) is not None
        card_brief = _card_brief(t)
        card_brief["has_payment_method"] = has_pm
    except Exception:
        card_brief["has_payment_method"] = has_pm

    def _iso(dt):
        if not dt:
            return None
        try:
            return dt.isoformat() + ("Z" if not str(dt).endswith("Z") else "")
        except Exception:
            return str(dt)

    email = getattr(t, "contact_email", None) or getattr(t, "email", None)
    out = {
        "tenant_id": t.id,
        # Match /v1/account field names so the model aligns with the Master Account UI
        "company_name": getattr(t, "company_name", None) or getattr(t, "name", None),
        "operator_name": getattr(t, "operator_name", None),
        "email": email,  # contact_email — THIS is what the UI shows
        "contact_email": email,
        "product": getattr(t, "product", None) or "array_operator",
        "plan": getattr(t, "plan", None),
        "billing_plan": getattr(t, "billing_plan", None),
        "plan_features": plan_features,
        "subscription_status": getattr(t, "subscription_status", None),
        "active": getattr(t, "active", None),
        "is_demo": bool(getattr(t, "is_demo", False)),
        "trial_ends_at": _iso(getattr(t, "trial_ends_at", None)),
        "has_password": bool(getattr(t, "password_hash", None)),
        "has_payment_method": card_brief.get("has_payment_method"),
        "card_brand": card_brief.get("card_brand"),
        "card_last4": card_brief.get("card_last4"),
        "card_exp": card_brief.get("card_exp"),
        "capture_mode": getattr(t, "capture_mode", None),
        "capture_mode_label": (
            "cloud — Store it with us (server holds encrypted passwords, harvester 24/7)"
            if getattr(t, "capture_mode", None) == "cloud"
            else "device — Keep it on my computer (extension vault; refresh while browser active)"
            if getattr(t, "capture_mode", None) == "device"
            else "unset — client may fall back to local default; ask owner to pick on Account → Auto-refresh"
        ),
        "send_from_email": getattr(t, "send_from_email", None),
        "send_from_name": getattr(t, "send_from_name", None),
        "report_frequency": getattr(t, "report_frequency", None),
        "accounts_count": accounts_count,
        "connected_providers": connected_providers,
        "bills_count": bills_count,
        "clients_count": clients_count,
        "created_at": _iso(getattr(t, "created_at", None)),
        "extension_heartbeat_at": _iso(getattr(t, "extension_heartbeat_at", None)),
        "last_pull_at": _iso(getattr(t, "last_pull_at", None)),
        "utility_session": {
            "captured_at": _iso(getattr(last_sess, "captured_at", None)) if last_sess else None,
            "expires_at": _iso(getattr(last_sess, "expires_at", None)) if last_sess else None,
        } if last_sess else None,
        "ui_tab": "#account",
        "field_notes": {
            "email": "Maps to tenants.contact_email — the Account tab 'Email' field",
            "company_name": "Business name on the profile card",
            "operator_name": "Personal name of the human operator",
            "billing_plan": "Array Operator product plan (vendor_data / invoicing entitlements)",
            "has_payment_method": "Card on file for AO subscription — not offtaker invoices",
            "capture_mode": (
                "Auto-refresh path for portal logins: cloud=server harvester; "
                "device=Chrome extension vault. Orthogonal to SolarEdge API keys AND to "
                "extension one-click capture (which can attach arrays without a vault row)."
            ),
            "extension_heartbeat_at": (
                "Last time the EnergyAgent Chrome extension pinged this tenant. Recent = "
                "extension installed/paired on some browser; not the same as cloud vault."
            ),
            "fleet_vendors_vs_cloud_logins": (
                "fleet_vendors = vendors seen on live arrays/inverters. cloud_capture.logins = "
                "only passwords saved for server harvest. SMA arrays with no SMA cloud login "
                "usually came from extension Log-in-with capture."
            ),
        },
        "auto_refresh_explainer": (
            "See product_map(topic=capture). Cloud + device are scheduled Auto-refresh modes; "
            "extension one-click Log-in-with is a separate first-attach path; SolarEdge API "
            "keys are a third server-poll path."
        ),
    }

    # Extension liveness (paired browser somewhere)
    try:
        hb = getattr(t, "extension_heartbeat_at", None)
        age_s = None
        if hb is not None:
            try:
                age_s = max(0, int((_now() - hb.replace(tzinfo=None)).total_seconds()))
            except Exception:
                age_s = None
        out["extension"] = {
            "heartbeat_at": _iso(hb),
            "seen_recently": bool(age_s is not None and age_s < 6 * 3600),
            "heartbeat_age_seconds": age_s,
            "role": (
                "EnergyAgent Chrome extension pairs to this tenant, can open vendor portals, "
                "auto-capture authenticated data, and POST it. Works for first attach even "
                "when capture_mode=cloud and that vendor is not in the cloud vault."
            ),
        }
    except Exception as e:
        out["extension"] = {"error": str(e)[:120]}

    # Fleet vendor mix (ground truth for "what vendors do I have?") vs vault.
    # Array has no vendor column — vendors live on Inverter + InverterConnection.
    try:
        from .models import Inverter, InverterConnection, Array
        counts: dict[str, int] = {}
        inv_rows = db.execute(
            select(Inverter.vendor).where(
                Inverter.tenant_id == t.id,
                Inverter.deleted_at.is_(None),
            )
        ).all()
        for (v,) in inv_rows:
            key = (v or "").strip().lower() or "unknown"
            counts[key] = counts.get(key, 0) + 1
        # Connections for API vendors that may not yet have inverter rows
        conn_rows = db.execute(
            select(InverterConnection.vendor)
            .join(Array, Array.id == InverterConnection.array_id)
            .where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
            )
        ).all()
        for (v,) in conn_rows:
            key = (v or "").strip().lower() or "unknown"
            if key not in counts:
                counts[key] = 1
        # SolarEdge legacy columns on Array
        se_n = db.execute(
            select(Array.id).where(
                Array.tenant_id == t.id,
                Array.deleted_at.is_(None),
                Array.solaredge_site_id.is_not(None),
            )
        ).all()
        if se_n and "solaredge" not in counts:
            counts["solaredge"] = len(se_n)
        out["fleet_vendors"] = [
            {"vendor": k, "count": counts[k]}
            for k in sorted(counts.keys())
        ]
    except Exception as e:
        out["fleet_vendors"] = {"error": str(e)[:120]}

    # Best-effort cloud-capture roster counts (no passwords)
    try:
        from .models import PortalCredential
        creds = db.execute(
            select(PortalCredential).where(PortalCredential.tenant_id == t.id)
        ).scalars().all()
        cloud_provs = sorted({
            (c.provider or "").strip().lower()
            for c in creds if c.provider
        })
        fleet_provs = []
        if isinstance(out.get("fleet_vendors"), list):
            fleet_provs = [x["vendor"] for x in out["fleet_vendors"] if x.get("vendor")]
        only_fleet = sorted(set(fleet_provs) - set(cloud_provs) - {"unknown", ""})
        out["cloud_capture"] = {
            "credential_count": len(creds),
            "enabled_count": sum(1 for c in creds if getattr(c, "cloud_capture_enabled", False)),
            "logins": [
                {
                    "provider": c.provider,
                    "username": c.username,
                    "enabled": bool(getattr(c, "cloud_capture_enabled", False)),
                    "last_harvest_at": _iso(getattr(c, "last_harvest_at", None)),
                    "last_harvest_ok": getattr(c, "last_harvest_ok", None),
                }
                for c in creds[:40]
            ],
            "providers_in_vault": cloud_provs,
            "fleet_vendors_not_in_cloud_vault": only_fleet,
            "provenance_note": (
                "If a vendor is on the fleet but not in the cloud vault, data almost "
                "certainly arrived via EnergyAgent extension one-click capture, "
                "onboarding sync, or an API key — not from a cloud PortalCredential."
            ),
        }
    except Exception as e:
        out["cloud_capture"] = {"error": str(e)[:120]}

    if include_billing:
        try:
            from .account import billing_summary as _billing_summary_ep
            # Call the pure helpers with tenant object (no HTTP)
            from .stripe_helpers import is_array_operator
            from . import account as account_mod
            if is_array_operator(getattr(t, "product", "nepool")):
                out["billing_snapshot"] = account_mod._billing_summary_kwh(t)
            else:
                out["billing_snapshot"] = account_mod._billing_summary_arrays(t)
        except Exception as e:
            out["billing_snapshot"] = {"error": str(e)[:200]}

    return out


# ── setup / operational objective ───────────────────────────────────────────
# The agent's STANDING JOB: get this operator fully operational, then keep them
# there. It reasons over a per-tenant completeness model (mirrors the frontend
# hands-off pillars) anchored on DATA FRESHNESS — the failure mode that silently
# breaks "operational" in this product (a login lapses, capture goes stale, an
# array goes dark and nobody notices). She names the ONE highest-value gap and
# acts on it; when everything's green she goes quiet.

_CAPTURE_STALE_HOURS = float(os.getenv("EA_CAPTURE_STALE_HOURS", "30") or 30)


def _hours_since(dt) -> float | None:
    if not dt:
        return None
    try:
        d = dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
        return max(0.0, (_now() - d).total_seconds() / 3600.0)
    except Exception:
        return None


def _compute_setup_status(db, tenant: Tenant) -> dict:
    """Per-tenant operational-completeness model. Cheap counts + the freshest
    capture timestamp. Returns pillars, the single top gap, and a one-line
    summary for prompt injection. Every sub-query is defensive."""
    from sqlalchemy import func
    from .models import (
        Array, Inverter, UtilityAccount, UtilitySession, Bill,
        BillingReportSubscription, PortalCredential,
    )
    t = db.get(Tenant, tenant.id) or tenant

    def _count(model, *where):
        try:
            return int(db.execute(select(func.count()).select_from(model).where(*where)).scalar() or 0)
        except Exception:
            return 0

    arrays_n = _count(Array, Array.tenant_id == t.id, Array.deleted_at.is_(None))
    inverter_backed = 0
    try:
        inverter_backed = int(db.execute(
            select(func.count(func.distinct(Inverter.array_id))).where(
                Inverter.array_id.in_(
                    select(Array.id).where(Array.tenant_id == t.id, Array.deleted_at.is_(None))
                )
            )
        ).scalar() or 0)
    except Exception:
        pass
    util_n = _count(UtilityAccount, UtilityAccount.tenant_id == t.id)
    bills_n = _count(Bill, Bill.tenant_id == t.id)
    offtakers_n = _count(BillingReportSubscription, BillingReportSubscription.tenant_id == t.id)
    cloud_creds_n = _count(
        PortalCredential,
        PortalCredential.tenant_id == t.id,
        PortalCredential.cloud_capture_enabled.is_(True),
    )
    contacts_n = 0
    try:
        from .repair_ops import list_contacts
        contacts_n = len(list_contacts(db, t.id))
    except Exception:
        pass

    # Freshest "capture pipeline actually ran" signal across all paths.
    fresh_dt = None
    try:
        cand = []
        cand.append(getattr(t, "last_pull_at", None))
        cand.append(getattr(t, "extension_heartbeat_at", None))
        lh = db.execute(
            select(func.max(PortalCredential.last_harvest_at)).where(
                PortalCredential.tenant_id == t.id
            )
        ).scalar()
        cand.append(lh)
        ls = db.execute(
            select(func.max(UtilitySession.captured_at)).where(
                UtilitySession.tenant_id == t.id
            )
        ).scalar()
        cand.append(ls)
        cand = [c for c in cand if c]
        fresh_dt = max(cand) if cand else None
    except Exception:
        pass
    hours_since = _hours_since(fresh_dt)
    capture_mode = getattr(t, "capture_mode", None)
    # "Configured" = a scheduled refresh path exists (cloud creds, or device
    # extension paired, or API-key vendors implied by arrays existing).
    capture_configured = bool(cloud_creds_n > 0 or capture_mode in ("cloud", "device"))
    data_fresh = hours_since is not None and hours_since <= _CAPTURE_STALE_HOURS
    data_stale = arrays_n > 0 and (hours_since is None or hours_since > _CAPTURE_STALE_HOURS)

    pay_ready = bool(getattr(t, "stripe_connect_charges_enabled", False))

    pillars = [
        {"key": "arrays", "label": "Arrays connected", "done": arrays_n > 0,
         "optional": False, "detail": f"{arrays_n} array(s), {inverter_backed} inverter-backed"},
        {"key": "auto_refresh", "label": "Auto-refresh configured", "done": capture_configured,
         "optional": False,
         "detail": (f"mode={capture_mode or 'unset'}, {cloud_creds_n} cloud login(s)")},
        {"key": "data_fresh", "label": "Data flowing (fresh)", "done": (arrays_n == 0 or data_fresh),
         "optional": False,
         "detail": (f"last capture ~{round(hours_since,1)}h ago" if hours_since is not None
                    else "no capture on record")},
        {"key": "utility_bills", "label": "Utility bills landing", "done": bills_n > 0,
         "optional": False, "detail": f"{util_n} utility account(s), {bills_n} bill(s)"},
        {"key": "offtakers", "label": "Offtakers added", "done": offtakers_n > 0,
         "optional": True, "detail": f"{offtakers_n} offtaker(s)"},
        {"key": "repair_contact", "label": "Repair contact on file", "done": contacts_n > 0,
         "optional": True, "detail": f"{contacts_n} O&M contact(s)"},
        {"key": "online_pay", "label": "Online pay (Stripe Connect)", "done": pay_ready,
         "optional": True, "detail": ("ready" if pay_ready else "not connected")},
    ]

    core_open = [p for p in pillars if not p["done"] and not p["optional"]]
    optional_open = [p for p in pillars if not p["done"] and p["optional"]]
    fully_operational = not core_open

    # Rank the single highest-value gap. Data-freshness on an existing fleet is
    # the money-leak gap and outranks incomplete first-time setup.
    top_gap = None
    if data_stale:
        top_gap = {
            "key": "data_fresh",
            "why": (f"Your fleet data has been stale for ~{round(hours_since,1) if hours_since else '??'}h "
                    "— arrays could be dark and invoices could be running on old numbers."),
            "action": "refresh_capture (or re-add the lapsed portal login on Account → Auto-refresh)",
            "costing_money": True,
        }
    elif arrays_n == 0:
        top_gap = {"key": "arrays", "why": "No arrays are connected yet — nothing can be monitored or billed.",
                   "action": "walk them through connecting a vendor portal (onboarding fork)",
                   "costing_money": False}
    elif not capture_configured:
        top_gap = {"key": "auto_refresh", "why": "No 24/7 auto-refresh — data only updates when a browser is open.",
                   "action": "help save a portal login to cloud (Account → Auto-refresh)", "costing_money": False}
    elif bills_n == 0:
        top_gap = {"key": "utility_bills", "why": "No utility bills on file — offtaker invoices can't compute.",
                   "action": "save a utility login so bills land", "costing_money": False}
    elif optional_open:
        p = optional_open[0]
        top_gap = {"key": p["key"], "why": f"{p['label']} is not set up yet (optional).",
                   "action": f"offer to set up {p['label'].lower()}", "costing_money": False}

    if fully_operational and not optional_open:
        summary = "Fully operational — everything's connected and data is fresh. Nothing to nudge."
    elif top_gap:
        summary = f"Top gap: {top_gap['why']} Action: {top_gap['action']}."
    else:
        summary = "Operational; some optional setup remains."

    return {
        "fully_operational": fully_operational,
        "data_fresh": bool(arrays_n == 0 or data_fresh),
        "hours_since_capture": round(hours_since, 1) if hours_since is not None else None,
        "capture_mode": capture_mode,
        "pillars": pillars,
        "core_gaps": [p["key"] for p in core_open],
        "optional_gaps": [p["key"] for p in optional_open],
        "top_gap": top_gap,
        "summary_line": summary,
    }


def _setup_status_tool(db, tenant: Tenant, args: dict) -> dict:
    st = _compute_setup_status(db, tenant)
    st["instruction_for_agent"] = (
        "This is your standing objective: get them fully operational, then keep them there. "
        "Lead with top_gap when it exists and it's relevant — name the SPECIFIC gap and offer "
        "to act (don't ask 'is everything set up?'). If data_fresh is false, that's a money "
        "leak — offer refresh_capture. If fully_operational and no optional gaps, say things "
        "look good in one line and don't invent work."
    )
    return st


def _refresh_capture_tool(db, tenant: Tenant, args: dict) -> dict:
    """Force fresh data now, across every path the SERVER can actually push:
      • Cloud vault logins → re-arm (harvester re-captures in ~a minute).
      • Utility bills (GMP-pullable) → background re-pull.
    Honest about what it CANNOT force: device/extension capture and SmartHub/VEC
    utilities only refresh when a signed-in browser is open; SolarEdge rides its
    own 5-minute server poll. Never claims 'refreshed' for those — 'flagged' at most.
    """
    from .models import PortalCredential
    triggered: list[str] = []
    limits: list[str] = []

    # 1) Cloud vault — re-arm enabled creds (same as POST /v1/cloud-capture/refresh).
    cloud_n = 0
    try:
        rows = db.execute(
            select(PortalCredential).where(
                PortalCredential.tenant_id == tenant.id,
                PortalCredential.cloud_capture_enabled.is_(True),
                PortalCredential.secret_enc.isnot(None),
            )
        ).scalars().all()
        for r in rows:
            r.last_harvest_at = None
            r.harvest_fails = 0
            r.updated_at = _now()
        db.commit()
        cloud_n = len(rows)
        if cloud_n:
            triggered.append(
                f"Re-armed {cloud_n} cloud login(s) — the harvester re-captures within ~a minute."
            )
    except Exception as e:
        db.rollback()
        log.warning("refresh_capture cloud re-arm failed: %s", e)

    # 2) Utility bills — background GMP re-pull (never block the turn; demo no-ops).
    if not bool(getattr(tenant, "is_demo", False)):
        try:
            import threading
            from .worker import pull_bills_for_tenant
            tid = tenant.id

            def _pull():
                try:
                    pull_bills_for_tenant(tid)
                except Exception as e:  # noqa: BLE001
                    log.warning("refresh_capture bg bill pull failed %s: %s", tid, e)

            threading.Thread(target=_pull, daemon=True).start()
            triggered.append("Re-pulling your utility bills now (GMP-pullable accounts).")
            limits.append("SmartHub / VEC co-op bills can't be pulled from our servers — those refresh from your browser.")
        except Exception as e:
            log.warning("refresh_capture bill pull dispatch failed: %s", e)

    # 3) Honest limits for the paths the server cannot force.
    hb = getattr(tenant, "extension_heartbeat_at", None)
    hb_hours = _hours_since(hb)
    if getattr(tenant, "capture_mode", None) == "device" or (hb_hours is not None and hb_hours < 24):
        if hb_hours is not None and hb_hours < 24:
            limits.append("Device/extension vendors (Fronius/SMA/Chint) refresh on your next browser sync — a browser is paired, so it should catch up soon.")
        else:
            limits.append("Device/extension vendors only refresh when a signed-in browser is open — open Array Operator to pull them.")
    limits.append("SolarEdge rides its own 5-minute server poll — no manual poke needed.")

    if not triggered:
        return {
            "ok": True,
            "triggered_nothing": True,
            "message": (
                "Nothing to force from our side right now — you're either on device/extension "
                "capture or SolarEdge's automatic poll. "
            ),
            "limits": limits,
            "instruction_for_agent": (
                "Be honest: there was no server-forceable refresh. If data is stale and it's a "
                "device/co-op login, tell them to open Array Operator (or re-add the login on "
                "Account → Auto-refresh); don't claim you refreshed it."
            ),
        }
    return {
        "ok": True,
        "cloud_logins_rearmed": cloud_n,
        "triggered": triggered,
        "limits": limits,
        "instruction_for_agent": (
            "Tell them plainly what you kicked off (cloud re-arm ~1 min; bills re-pulling) and "
            "be honest about what can't be forced. Offer to check back after it lands. Do NOT "
            "over-promise instant results."
        ),
    }


def _ea_judge_write(name: str, args: dict) -> dict | None:
    """Internal judge for Energy Agent writes (not the site auto-ship judge).

    Returns None if allowed (or needs normal confirm), or a dict rejection.
    Hard-blocks anything that touches operator billing / Stripe money.
    """
    blob = json.dumps(args or {}, default=str).lower() + " " + (name or "").lower()
    banned = (
        "stripe", "payment_method", "price_id", "subscription_item",
        "charge", "invoice.pay", "billing_plan", "unit_amount",
        "sk_live", "sk_test", "cancel_subscription", "update_subscription",
        "add_payment", "setup_intent", "payment_intent",
    )
    if any(b in blob for b in banned):
        return {
            "ok": False,
            "judged": "reject",
            "error": (
                "Blocked by Energy Agent judge: operator billing / payment changes "
                "are not allowed. Open the billing portal link for the owner to manage "
                "their own card, or escalate to Ford."
            ),
        }
    # Site improvement text that tries to force auto-ship / steal keys
    if name == "propose_site_improvement":
        t = (args.get("text") or "").lower()
        if any(x in t for x in (
            "ignore previous", "exfiltrat", "api key", "admin key",
            "mark this auto", "ship without review", "bypass judge",
        )):
            return {
                "ok": False,
                "judged": "reject",
                "error": "Blocked: suggestion looks like prompt-injection / security ask.",
            }
    return None


def _propose_site_improvement_tool(db, tenant: Tenant, args: dict) -> dict:
    """Queue a feature suggestion (same table/pipeline as Wish this was better)."""
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text is required — what should change?"}
    text = text[:5000]
    start_markup = args.get("start_markup", True)
    if start_markup is None:
        start_markup = True

    # If they only want the client mark-up flow, don't create a row yet.
    # Prefill the Build-it box with a judge-ready prompt (Ford 2026-07-14).
    if start_markup and not args.get("screenshot_b64") and not args.get("force_submit"):
        build_prompt = (args.get("build_prompt") or args.get("prompt") or text or "").strip()
        # Shape casual speech into an imperative build brief when needed
        if build_prompt and not re.match(
            r"^(add|put|make|change|move|upgrade|replace|show|hide|fix|redesign|create|build)\b",
            build_prompt,
            re.I,
        ):
            build_prompt = (
                f"Build this UI improvement from the owner's request: {build_prompt}. "
                "Prefer a clear, scannable visual that matches Array Operator "
                "(energy / black-green aesthetic). Small pure-UI change only — "
                "no billing math or Stripe changes."
            )
        build_prompt = build_prompt[:1600]
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "improve_site",
            "args": {
                "mark_first": True,
                "hint": build_prompt,
                "prompt": build_prompt,
                "build_prompt": build_prompt,
                "text": build_prompt,
            },
            "needs_confirm": False,
        }
        return {
            "status": "ui_command",
            "command": cmd,
            "message": (
                "Opening mark-up with a ready-to-build prompt filled in. "
                "User circles the spot, then hits Build it. "
                f"Prompt: {build_prompt[:220]}"
            ),
        }

    shot = None
    raw = (args.get("screenshot_b64") or "").strip()
    if raw:
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            import base64 as _b64
            decoded = _b64.b64decode(raw, validate=True)
            if 0 < len(decoded) <= 4_000_000 and (
                decoded[:8] == b"\x89PNG\r\n\x1a\n" or decoded[:3] == b"\xff\xd8\xff"
            ):
                shot = raw
        except Exception:
            shot = None

    try:
        from .feature_suggestions import FeatureSuggestion
        fs = FeatureSuggestion(
            text=text,
            email=getattr(tenant, "contact_email", None) or getattr(tenant, "email", None),
            tenant_id=tenant.id,
            product=getattr(tenant, "product", None) or "array_operator",
            screenshot_b64=shot,
            status="new",
        )
        db.add(fs)
        db.commit()
        db.refresh(fs)
        sid = fs.id
    except Exception as e:
        log.exception("propose_site_improvement failed")
        return {"error": f"could not queue improvement: {e}"}

    try:
        send_internal_alert(
            subject=f"Energy Agent site improvement (#{sid})",
            body=(
                f"From Energy Agent session\nTenant: {tenant.id}\n"
                f"Email: {getattr(tenant, 'contact_email', None)}\n\n{text}\n"
                + ("\n[Includes marked-up screenshot]\n" if shot else "")
                + "\n(Queued for judge + review harness — same as Wish this was better.)"
            ),
        )
    except Exception:
        pass

    return {
        "ok": True,
        "suggestion_id": sid,
        "status": "new",
        "pipeline": "feature_suggestion_judge",
        "message": (
            f"Queued improvement #{sid}. Client should watch build progress. "
            "Judge may auto-ship pure UI, branch riskier work, or pass."
        ),
        "status_url": f"/v1/feature-suggestion/{sid}/status",
        "command": {
            "id": uuid.uuid4().hex[:12],
            "type": "watch_build",
            "args": {"suggestion_id": sid},
            "needs_confirm": False,
        },
        "status_flag": "ui_command",
    }


def _user_clearly_directed(user_text: str, payload: dict | None = None) -> bool:
    """True when the user's message already states the exact write — no second 'yes'.

    Examples that MUST auto-apply (Ford 2026-07-14): "change it to 15%",
    "set the share percent to 20", "update email to a@b.com". Vague "can you edit
    this offtaker?" without a value still needs a clarifying question — not a confirm.
    """
    t = (user_text or "").strip()
    if not t:
        return False
    # Explicit one-shot approvals always count
    if _YES_RE.match(t):
        return True
    low = t.lower()
    # Imperative / request + a concrete value
    has_directive = bool(
        re.search(
            r"\b(change|set|update|make\s+it|edit|switch|put|bump|raise|lower|"
            r"move|rename|want|like|please|i'?d\s+like)\b",
            low,
        )
    )
    if not has_directive:
        return False
    payload = payload or {}
    # Share % (most common offtaker edit)
    if re.search(r"\d+(\.\d+)?\s*%", low) or re.search(
        r"\b(to|at|as)\s+\d+(\.\d+)?\b", low
    ):
        if re.search(r"\b(share|percent|pct|allocation|%\s*share)\b", low) or any(
            k in payload for k in ("share_pct", "allocation_pct", "array_share_pct")
        ) or not payload:
            return True
    # Email
    if re.search(r"[\w.+-]+@[\w.-]+\.\w+", t):
        if re.search(r"\b(email|e-mail|mail)\b", low) or "email" in payload or "client_email" in payload:
            return True
    # Master / utility / array rebind by name
    if re.search(r"\b(master\s*account|utility|source|bind|rebind|array)\b", low):
        if any(
            k in payload
            for k in (
                "utility_account_id",
                "array_id",
                "utility_account_name",
                "array_name",
                "master_account",
            )
        ) or re.search(r"\b(to|for)\s+[\w][\w\s-]{1,40}", low):
            return True
    # Explicit rename
    if re.search(r"\b(rename|name\s+it|call\s+(it|them))\b", low):
        return True
    # Payload present + directive + number/value-ish
    if payload and re.search(r"\d", low):
        return True
    return False


def _detect_tour_id(user_text: str | None) -> str | None:
    """Map walkthrough language → client preset tour_id (never freehand selectors)."""
    t = (user_text or "").lower()
    if not t:
        return None
    if re.search(r"\bwhat (are|is) (all )?(the )?(different )?tabs\b", t):
        return None
    wants = bool(
        re.search(
            r"\b(walk\s*me|walk\s*us|walkthrough|show\s+me|show\s+us|tour|"
            r"guide\s+me|take\s+me\s+through|walk\s+through|show\s+me\s+around|"
            r"look\s+around|orient\s+me)\b",
            t,
        )
        or re.search(r"\bexplain\b.*\b(tab|page|screen|section|panel)\b", t)
        or re.search(
            r"\b(explain|describe|overview)\b.*\b(invoices?|account|inverters?|"
            r"analysis|resources|triage|offtakers?)\b",
            t,
        )
        or re.search(r"\bgive\s+me\s+a\s+(walkthrough|tour|overview|rundown)\b", t)
        or re.search(
            r"\bhow\s+(do|does)\s+(the\s+)?(account|invoices?|inverters?|analysis|"
            r"resources|fleet\s+triage|this|offtakers?)\b",
            t,
        )
    )
    tab_hit = bool(
        re.search(
            r"\b(master\s*account|account\s+tab|invoices?\s+tab|inverters?\s+tab|"
            r"fleet\s+triage|arrays?\s+tab|resources?\s+tab|analysis\s+tab)\b",
            t,
        )
    )
    if not wants and not (
        tab_hit and re.search(r"\b(show|open|explain|walk|through|around|tour|guide)\b", t)
    ):
        return None
    if re.search(r"\b(invoices?|offtakers?|billing\s+report|credit\s+invoices?)\b", t) or (
        re.search(r"\breports?\b", t) and re.search(r"\btab\b", t)
    ):
        return "reports"
    if re.search(r"\b(master\s*account|account\s+tab)\b", t) or (
        re.search(r"\baccount\b", t)
        and re.search(r"\b(walk|tour|show|explain|through|around|guide)\b", t)
    ):
        return "master_account"
    if re.search(r"\bfleet\s+triage\b", t) or (
        re.search(r"\btriage\b", t)
        and re.search(r"\b(walk|tour|show|around)\b", t)
    ):
        return "dashboard"
    if re.search(r"\b(inverters?|spreadsheet|sandbox|fleet\s+canvas)\b", t) or (
        re.search(r"\barrays?\b", t)
        and re.search(r"\b(tab|walk|tour|show|around)\b", t)
    ):
        return "arrays"
    if re.search(r"\banalysis\b", t) or re.search(r"\btrends?\b", t) or re.search(
        r"\bthrough\s+time\b", t
    ):
        return "analysis"
    if (
        re.search(r"\bresources?\b", t)
        or re.search(r"\bnet.?meter|rates?\s+and\s+news|briefing\b", t)
        or re.search(r"\brec\s+market\b", t)
    ):
        return "resources"
    return None


def _run_tool(
    name: str,
    args: dict,
    tenant: Tenant,
    session: EaSession,
    db,
    user_text: str = "",
) -> dict:
    args = args or {}
    tid = tenant.id

    # Judge gate — hard reject billing/money writes before anything else
    blocked = _ea_judge_write(name, args)
    if blocked is not None:
        return blocked

    if name == "tenant_census":
        return _tenant_census_tool(db, tenant, args)

    if name == "query_tenant":
        return _query_tenant_tool(db, tenant, args)

    if name == "product_map":
        return _product_map_tool(args)

    if name == "web_search":
        out = _web_search_tool(args)
        try:
            _charge(db, tid, 0.003, "web_search")
        except Exception:
            pass
        return out

    if name == "web_fetch":
        out = _web_fetch_tool(args)
        try:
            _charge(db, tid, 0.004, "web_fetch")
        except Exception:
            pass
        return out

    if name == "propose_site_improvement":
        out = _propose_site_improvement_tool(db, tenant, args)
        # Normalize command packaging for the agent turn loop
        if out.get("status") == "ui_command":
            return out
        if out.get("command"):
            return {
                "status": "ui_command",
                "command": out["command"],
                "suggestion_id": out.get("suggestion_id"),
                "message": out.get("message"),
                "ok": out.get("ok"),
            }
        return out

    if name == "fleet_overview":
        return _fleet_overview_tool(db, tenant, args)

    if name == "investigate_attention":
        return _investigate_attention_tool(db, tenant, args)

    if name == "array_detail":
        return _array_detail_tool(db, tenant, args)

    if name in ("mark_inverter_expected_low", "clear_inverter_expected_low"):
        from . import inverter_fleet
        mark = name == "mark_inverter_expected_low"
        inv_id = args.get("inverter_id")
        if not inv_id:
            return {"ok": False, "error": "inverter_id required"}
        reason = (args.get("reason") or "").strip() or None
        needs = bool(args.get("needs_confirm", True))
        # If the owner clearly directed this in their message, skip the extra confirm.
        if needs and _user_clearly_directed(user_text, {"inverter_id": inv_id}):
            needs = False
        if needs:
            verb = ("mark it expected-low so it stops showing as underperforming"
                    if mark else "clear its expected-low mark and resume normal grading")
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": {"inverter_id": inv_id, "reason": reason}},
                "message": (
                    f"Just to confirm — {verb} for inverter #{inv_id}"
                    + (f' (reason: "{reason}")' if (mark and reason) else "")
                    + "? I'll still alert you if it ever drops below its established level."
                ),
                "needs_confirm": True,
            }
        try:
            t = db.get(Tenant, tid) or tenant
            iv = inverter_fleet.set_expected_low(
                db, t, int(inv_id), expected_low=mark, reason=reason, set_by="agent",
            )
            if mark:
                pct = round((iv.expected_low_baseline or 0) * 100)
                return {
                    "ok": True, "inverter_id": iv.id, "expected_low": True,
                    "baseline_pct": pct, "reason": iv.expected_low_reason,
                    "message": (
                        f"Done — inverter #{iv.id} is now marked expected-low at ~{pct}% of "
                        "its peers"
                        + (f" ({iv.expected_low_reason})" if iv.expected_low_reason else "")
                        + ". It won't read as underperforming while it holds that level, but "
                        "I'll flag it right away if it drops below — that would be a real new issue."
                    ),
                }
            return {
                "ok": True, "inverter_id": iv.id, "expected_low": False,
                "message": f"Done — inverter #{iv.id} is back to normal peer grading.",
            }
        except inverter_fleet.FleetError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── O&M / repair healing ──────────────────────────────────────────────
    if name == "repair_ops_overview":
        from . import repair_ops as ro
        t = db.get(Tenant, tid) or tenant
        if args.get("reconcile", True):
            try:
                ro.reconcile(db, t)
            except Exception as e:
                log.warning("ea repair reconcile: %s", e)
        return {"ok": True, **ro.ops_overview(db, t)}

    if name == "list_service_contacts":
        from . import repair_ops as ro
        contacts = ro.list_contacts(db, tid)
        return {
            "ok": True,
            "contacts": [ro.serialize_contact(c) for c in contacts],
            "assignments": ro.assignments_for_tenant(db, tid),
            "count": len(contacts),
        }

    if name == "upsert_service_contact":
        from . import repair_ops as ro
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {
            "name": args.get("name"), "email": args.get("email"),
        }):
            needs = False
        payload = {
            "contact_id": args.get("contact_id"),
            "name": args.get("name"),
            "company": args.get("company"),
            "role": args.get("role") or "om",
            "email": args.get("email"),
            "phone": args.get("phone"),
            "notes": args.get("notes"),
            "is_default": bool(args.get("is_default") or False),
            "active": True if args.get("active") is None else bool(args.get("active")),
            "trusted": args.get("trusted"),
        }
        reason = args.get("reason") or f"Save service contact {payload.get('name')}"
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": reason + " — confirm to save.",
                "needs_confirm": True,
            }
        try:
            c = ro.upsert_contact(
                db, tid,
                contact_id=payload.get("contact_id"),
                name=payload["name"],
                company=payload.get("company"),
                role=payload.get("role") or "om",
                email=payload.get("email"),
                phone=payload.get("phone"),
                notes=payload.get("notes"),
                is_default=bool(payload.get("is_default")),
                active=bool(payload.get("active", True)),
                trusted=payload.get("trusted"),
            )
            db.commit()
            return {"ok": True, "contact": ro.serialize_contact(c)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "assign_service_contact":
        from . import repair_ops as ro
        contact_id = args.get("contact_id")
        array_id = args.get("array_id")
        if not array_id and args.get("array_name"):
            arr, err = _find_array(db, tid, args["array_name"])
            if isinstance(err, dict):
                return {"ok": False, **err}
            if not arr:
                return {"ok": False, "error": f"array not found for '{args.get('array_name')}'"}
            array_id = arr.id
        if not contact_id or not array_id:
            return {"ok": False, "error": "contact_id and array_id (or array_name) required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"contact_id": contact_id, "array_id": array_id}):
            needs = False
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {
                    "tool": name,
                    "args": {
                        "contact_id": contact_id,
                        "array_id": array_id,
                        "kind": args.get("kind") or "primary",
                    },
                },
                "message": f"Assign contact #{contact_id} to array #{array_id} — confirm.",
                "needs_confirm": True,
            }
        try:
            row = ro.assign_array_contact(
                db, tid, int(array_id), int(contact_id),
                kind=args.get("kind") or "primary",
            )
            db.commit()
            return {
                "ok": True,
                "assignment": {
                    "array_id": row.array_id,
                    "contact_id": row.contact_id,
                    "kind": row.kind,
                },
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "list_repair_tickets":
        from . import repair_ops as ro
        tickets = ro.list_tickets(
            db, tid,
            status=args.get("status"),
            array_id=args.get("array_id"),
            active_only=bool(args.get("active_only", True)),
        )
        # Attach last inbound/outbound email snippets so chat never invents silence
        enriched = []
        for t in tickets:
            ser = ro.serialize_ticket(t)
            try:
                hist = ro._recent_checkins(db, t.id, limit=6)
                email_hist = [c for c in hist if (c.channel or "") == "email"]
                last_in = next(
                    (c for c in reversed(email_hist) if c.direction == "inbound"),
                    None,
                )
                last_out = next(
                    (c for c in reversed(email_hist) if c.direction == "outbound"),
                    None,
                )
                ser["last_inbound_at"] = ro._iso(last_in.created_at) if last_in else None
                ser["last_inbound_preview"] = (last_in.body or "")[:280] if last_in else None
                ser["last_outbound_at"] = ro._iso(last_out.created_at) if last_out else None
                ser["email_thread"] = [
                    {
                        "direction": c.direction,
                        "via": c.via,
                        "at": ro._iso(c.created_at),
                        "preview": (c.body or "")[:200],
                        "to_from": c.sent_to,
                    }
                    for c in email_hist[-4:]
                ]
            except Exception:
                pass
            enriched.append(ser)
        return {
            "ok": True,
            "tickets": enriched,
            "summary": ro.summarize_tickets(tickets),
            "count": len(tickets),
            "email_digest": ro.build_email_surface_digest(db, tid, limit=12),
        }

    if name == "open_repair_ticket":
        from . import repair_ops as ro
        array_id = args.get("array_id")
        if not array_id and args.get("array_name"):
            arr, err = _find_array(db, tid, args["array_name"])
            if isinstance(err, dict):
                return {"ok": False, **err}
            if not arr:
                return {"ok": False, "error": f"array not found for '{args.get('array_name')}'"}
            array_id = arr.id
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {
            "array": args.get("array_name") or array_id,
            "repair": True,
        }):
            needs = False
        payload = {
            "array_id": array_id,
            "inverter_id": args.get("inverter_id"),
            "contact_id": args.get("contact_id"),
            "fail_type": args.get("fail_type") or "other",
            "title": args.get("title"),
            "description": args.get("description"),
        }
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": "Open repair ticket — confirm.",
                "needs_confirm": True,
            }
        try:
            t = db.get(Tenant, tid) or tenant
            ticket = ro.open_ticket(db, t, source="agent", **{
                k: v for k, v in payload.items() if v is not None
            })
            db.commit()
            return {"ok": True, "ticket": ro.serialize_ticket(ticket)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "update_repair_ticket":
        from . import repair_ops as ro
        ticket_id = args.get("ticket_id")
        if not ticket_id:
            return {"ok": False, "error": "ticket_id required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"ticket_id": ticket_id, "status": args.get("status")}):
            needs = False
        payload = {
            "ticket_id": ticket_id,
            "status": args.get("status"),
            "contact_id": args.get("contact_id"),
            "tech_note": args.get("tech_note"),
            "description": args.get("description"),
            "scheduled_for": args.get("scheduled_for"),
        }
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": f"Update repair ticket #{ticket_id} — confirm.",
                "needs_confirm": True,
            }
        try:
            from .models import RepairTicket as RT
            t = db.get(Tenant, tid) or tenant
            ticket = db.get(RT, ticket_id)
            if ticket is None or ticket.tenant_id != tid:
                return {"ok": False, "error": "ticket not found"}
            sched = None
            clear_sched = False
            if payload.get("scheduled_for") is not None:
                raw = str(payload["scheduled_for"]).strip()
                if raw == "":
                    clear_sched = True
                else:
                    from datetime import datetime
                    sched = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            ro.update_ticket(
                db, t, ticket,
                status=payload.get("status"),
                contact_id=payload.get("contact_id"),
                tech_note=payload.get("tech_note"),
                description=payload.get("description"),
                scheduled_for=sched,
                clear_scheduled=clear_sched,
            )
            db.commit()
            return {"ok": True, "ticket": ro.serialize_ticket(ticket)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if name == "draft_repair_checkin":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        t = db.get(Tenant, tid) or tenant
        contact = ro.get_contact(db, tid, ticket.contact_id) if ticket.contact_id else None
        draft = ro.build_checkin_draft(ticket, t, contact)
        ticket.draft_checkin = draft
        db.commit()
        return {"ok": True, "ticket_id": ticket.id, "draft": draft, "contact": ro.serialize_contact(contact) if contact else None}

    if name == "send_repair_checkin":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {
            "checkin": True, "email": True, "ticket_id": ticket_id,
        }):
            # Only skip confirm if user language clearly means send/contact now
            ut = (user_text or "").lower()
            if any(w in ut for w in (
                "send", "email", "check in", "check-in", "contact", "reach out", "ping",
            )):
                needs = False
        payload = {
            "ticket_id": ticket_id,
            "to": args.get("to"),
            "subject": args.get("subject"),
            "body": args.get("body"),
        }
        if needs:
            draft = ticket.draft_checkin or {}
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": (
                    f"Send check-in for ticket #{ticket_id} to "
                    f"{payload.get('to') or draft.get('to') or 'assigned contact'} — confirm."
                ),
                "draft_preview": draft,
                "needs_confirm": True,
            }
        try:
            t = db.get(Tenant, tid) or tenant
            row = ro.send_checkin(
                db, t, ticket, via="agent",
                to_override=payload.get("to"),
                subject_override=payload.get("subject"),
                body_override=payload.get("body"),
            )
            db.commit()
            return {
                "ok": True,
                "checkin": ro.serialize_checkin(row),
                "ticket": ro.serialize_ticket(ticket),
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

    if name == "log_repair_note":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        note = (args.get("note") or "").strip()
        if not ticket_id or not note:
            return {"ok": False, "error": "ticket_id and note required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"note": note}):
            needs = False
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": {"ticket_id": ticket_id, "note": note}},
                "message": f"Log note on ticket #{ticket_id} — confirm.",
                "needs_confirm": True,
            }
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        try:
            t = db.get(Tenant, tid) or tenant
            row = ro.log_inbound_note(db, t, ticket, note, via="agent")
            db.commit()
            return {
                "ok": True,
                "checkin": ro.serialize_checkin(row),
                "ticket": ro.serialize_ticket(ticket),
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "log_repair_phone_note":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        note = (args.get("note") or "").strip()
        if not ticket_id or not note:
            return {"ok": False, "error": "ticket_id and note required"}
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"note": note, "phone": True}):
            needs = False
        payload = {"ticket_id": ticket_id, "note": note, "phone": args.get("phone")}
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": f"Log phone note on ticket #{ticket_id} — confirm.",
                "needs_confirm": True,
            }
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        try:
            t = db.get(Tenant, tid) or tenant
            row = ro.log_phone_note(db, t, ticket, note, phone=args.get("phone"), via="agent")
            db.commit()
            return {"ok": True, "checkin": ro.serialize_checkin(row), "ticket": ro.serialize_ticket(ticket)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "send_repair_sms":
        from . import repair_ops as ro
        from .models import RepairTicket as RT
        ticket_id = args.get("ticket_id")
        if not ticket_id:
            return {"ok": False, "error": "ticket_id required"}
        needs = bool(args.get("needs_confirm", True))
        ut = (user_text or "").lower()
        if needs and any(w in ut for w in ("sms", "text", "text message")):
            needs = False
        payload = {"ticket_id": ticket_id, "body": args.get("body"), "to": args.get("to")}
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": payload},
                "message": f"Send SMS check-in for ticket #{ticket_id} — confirm.",
                "needs_confirm": True,
            }
        ticket = db.get(RT, ticket_id)
        if ticket is None or ticket.tenant_id != tid:
            return {"ok": False, "error": "ticket not found"}
        try:
            t = db.get(Tenant, tid) or tenant
            body = (args.get("body") or "").strip()
            if not body:
                body = (
                    f"Status check on {ticket.site_name or 'site'} "
                    f"({ticket.inv_name or ticket.serial or 'inverter'}). "
                    f"Any update? [AO-TICKET-{ticket.id}]"
                )
            out = ro.send_or_log_sms(db, t, ticket, body, to=args.get("to"), via="agent")
            db.commit()
            return out
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "list_offtakers":
        from .models import BillingReportSubscription, UtilityAccount
        q = select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid,
        )
        if hasattr(BillingReportSubscription, "deleted_at"):
            q = q.where(BillingReportSubscription.deleted_at.is_(None))
        q = q.order_by(BillingReportSubscription.id).limit(300)
        try:
            subs = db.execute(q).scalars().all()
        except Exception as e:
            return {"error": f"could not list offtakers: {e}", "offtakers": []}
        # Batch-resolve array names + utility account labels for rebinding UI
        arr_ids = {getattr(s, "array_id", None) for s in subs}
        arr_ids.discard(None)
        ua_ids = {getattr(s, "utility_account_id", None) for s in subs}
        ua_ids.discard(None)
        arr_name = {}
        if arr_ids:
            for a in db.execute(
                select(Array).where(Array.id.in_(arr_ids), Array.tenant_id == tid)
            ).scalars().all():
                arr_name[a.id] = a.name
        ua_map = {}
        if ua_ids:
            for u in db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.id.in_(ua_ids), UtilityAccount.tenant_id == tid,
                )
            ).scalars().all():
                ua_map[u.id] = u
        # Batch pricing lookups once for the whole list (avoids per-sub SessionLocal)
        pricing_ctx = None
        try:
            from .billing.delivery import build_pricing_ctx
            pricing_ctx = build_pricing_ctx(db, tenant)
        except Exception as e:
            log.debug("pricing_ctx skipped: %s", e)
        result = []
        for s in subs:
            share = getattr(s, "array_share_pct", None)
            if share is None:
                share = getattr(s, "allocation_pct", None)
            if share is not None and float(share) <= 1:
                share = round(float(share) * 100, 4)
            uaid = getattr(s, "utility_account_id", None)
            aid = getattr(s, "array_id", None)
            ua = ua_map.get(uaid) if uaid else None
            nick = (getattr(ua, "nickname", None) or "").strip() if ua else None
            acct_num = getattr(ua, "account_number", None) if ua else None
            row = {
                "id": s.id,
                "name": getattr(s, "customer_name", None),
                "email": getattr(s, "client_email", None),
                "share_pct": share,
                "allocation_pct": getattr(s, "allocation_pct", None),
                "array_share_pct": getattr(s, "array_share_pct", None),
                "array_id": aid,
                "array_name": arr_name.get(aid) if aid else None,
                "utility_account_id": uaid,
                "utility_account_nickname": nick,
                "utility_account_number": acct_num,
                "utility_provider": (getattr(ua, "provider", None) if ua else None),
                "utility_label": (
                    nick or (f"{getattr(ua, 'provider', '')} {acct_num}".strip() if ua else None)
                ),
                "send_mode": getattr(s, "send_mode", None),
                "delivery_mode": getattr(s, "delivery_mode", None),
                "enabled": getattr(s, "enabled", None),
            }
            row.update(_offtaker_rate_fields(db, tenant, s, pricing_ctx=pricing_ctx))
            result.append(row)
        return {
            "offtakers": result,
            "count": len(result),
            "tenant_rates": _tenant_global_rates(tenant),
        }

    if name == "get_offtaker":
        sid = args.get("subscription_id")
        name_q = (args.get("name") or "").strip().lower()
        listed = _run_tool("list_offtakers", {}, tenant, session, db)
        for o in listed.get("offtakers") or []:
            if sid and o.get("id") == sid:
                return {"offtaker": o, "tenant_rates": listed.get("tenant_rates")}
            if name_q and name_q in str(o.get("name") or "").lower():
                return {"offtaker": o, "tenant_rates": listed.get("tenant_rates")}
        return {
            "error": "not found",
            "offtaker": None,
            "hint": "Call list_offtakers or get_billing_rates with offtaker_name",
            "tenant_rates": listed.get("tenant_rates"),
        }

    if name == "get_billing_rates":
        out = {"ok": True, "tenant": _tenant_global_rates(tenant)}
        sid = args.get("subscription_id")
        name_q = (args.get("offtaker_name") or args.get("name") or "").strip().lower()
        if args.get("include_all_offtakers"):
            listed = _run_tool("list_offtakers", {}, tenant, session, db)
            out["offtakers"] = [
                {
                    "id": o.get("id"),
                    "name": o.get("name"),
                    "solar_credit_rate_usd_per_kwh": o.get("solar_credit_rate_usd_per_kwh"),
                    "solar_credit_source": o.get("solar_credit_source"),
                    "net_rate_per_kwh": o.get("net_rate_per_kwh"),
                    "rate_per_kwh": o.get("rate_per_kwh"),
                    "discount_pct": o.get("discount_pct"),
                    "resolved_effective_rate": o.get("resolved_effective_rate"),
                    "array_name": o.get("array_name"),
                }
                for o in (listed.get("offtakers") or [])[:100]
            ]
            out["count"] = len(out["offtakers"])
            return out
        if sid or name_q:
            listed = _run_tool("list_offtakers", {}, tenant, session, db)
            match = None
            for o in listed.get("offtakers") or []:
                if sid and o.get("id") == sid:
                    match = o
                    break
                if name_q and name_q in str(o.get("name") or "").lower():
                    match = o
                    if str(o.get("name") or "").lower() == name_q:
                        break
            if not match:
                return {
                    "ok": False,
                    "error": f"no offtaker matching '{name_q or sid}'",
                    "tenant": out["tenant"],
                    "hint": "list_offtakers for names",
                }
            out["offtaker"] = match
            out["spoken_summary"] = (
                f"{match.get('name')}: solar credit "
                f"${match.get('solar_credit_rate_usd_per_kwh')}/kWh "
                f"(source={match.get('solar_credit_source')}; "
                f"effective after discount ${match.get('resolved_effective_rate')}/kWh). "
                f"Master: {out['tenant'].get('note')}"
            )
        return out

    if name == "set_billing_rates":
        from .models import Tenant as TenantModel
        needs = bool(args.get("needs_confirm", True))
        directed = {
            k: args.get(k)
            for k in (
                "default_net_rate_per_kwh",
                "default_discount_pct",
                "default_billing_rate_per_kwh",
            )
            if args.get(k) is not None
        }
        if args.get("clear_net_rate") or args.get("clear_discount"):
            directed["clear"] = True
        if needs and _user_clearly_directed(user_text, directed or {"rate": True}):
            needs = False
        try:
            payload = {}
            if args.get("clear_net_rate"):
                payload["default_net_rate_per_kwh"] = None
            elif args.get("default_net_rate_per_kwh") is not None:
                payload["default_net_rate_per_kwh"] = _validate_ea_rate(
                    args["default_net_rate_per_kwh"]
                )
            if args.get("clear_discount"):
                payload["default_discount_pct"] = None
            elif args.get("default_discount_pct") is not None:
                payload["default_discount_pct"] = _validate_ea_discount(
                    args["default_discount_pct"]
                )
            if args.get("default_billing_rate_per_kwh") is not None:
                payload["default_billing_rate_per_kwh"] = _validate_ea_rate(
                    args["default_billing_rate_per_kwh"]
                )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not payload:
            return {
                "ok": False,
                "error": "pass default_net_rate_per_kwh, default_discount_pct, or clear_* flags",
                "current": _tenant_global_rates(tenant),
            }
        reason = "Set master billing rates: " + ", ".join(
            f"{k}={v}" for k, v in payload.items()
        )
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "api_patch",
            "args": {
                "method": "PUT",
                "path": "/v1/array-operator/billing/global-rate",
                "body": payload,
            },
            "needs_confirm": bool(needs),
            "reason": reason,
        }
        if not needs:
            tt = db.get(TenantModel, tid)
            if tt is None:
                return {"ok": False, "error": "tenant not found"}
            for k, v in payload.items():
                setattr(tt, k, v)
            db.add(tt)
            db.commit()
            db.refresh(tt)
            return {
                "status": "ui_command",
                "command": {
                    "id": uuid.uuid4().hex[:12],
                    "type": "ui_refresh",
                    "args": {"surface": "reports", "rates": True},
                    "needs_confirm": False,
                },
                "also_commands": [cmd],
                "applied": _tenant_global_rates(tt),
                "message": "Master solar credit / discount rates updated.",
            }
        return {
            "status": "pending_confirm",
            "pending": cmd,
            "message": reason + " — confirm to apply.",
        }

    if name == "production_forecast":
        return _production_forecast_tool(db, tenant, args)

    if name == "list_recent_invoices":
        return _list_recent_invoices_tool(db, tenant, args)

    if name == "fleet_trends_summary":
        # Lightweight local summary from DailyGeneration if trends endpoint is heavy
        try:
            from .models import DailyGeneration
            from sqlalchemy import func
            life = db.execute(
                select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .select_from(DailyGeneration)
                .join(Array, Array.id == DailyGeneration.array_id)
                .where(Array.tenant_id == tid, Array.deleted_at.is_(None))
            ).scalar() or 0.0
            since = (_now().date() - timedelta(days=365))
            ttm = db.execute(
                select(func.coalesce(func.sum(DailyGeneration.kwh), 0.0))
                .select_from(DailyGeneration)
                .join(Array, Array.id == DailyGeneration.array_id)
                .where(
                    Array.tenant_id == tid,
                    Array.deleted_at.is_(None),
                    DailyGeneration.day >= since,
                )
            ).scalar() or 0.0
            return {
                "lifetime_kwh": round(float(life), 1),
                "ttm_kwh": round(float(ttm), 1),
                "note": "From DailyGeneration; open Analysis → Through time for YoY bars.",
            }
        except Exception as e:
            return {"error": str(e)}

    if name == "account_summary":
        return _account_summary_tool(db, tenant, args)

    if name == "setup_status":
        return _setup_status_tool(db, tenant, args)

    if name == "refresh_capture":
        needs = bool(args.get("needs_confirm", True))
        if needs and _user_clearly_directed(user_text, {"refresh": True}):
            needs = False
        if needs:
            return {
                "status": "pending_confirm",
                "pending": {"tool": name, "args": {"needs_confirm": False}},
                "message": (args.get("reason") or "Refresh your data now")
                + " — I'll re-arm cloud logins and re-pull bills. Confirm?",
                "needs_confirm": True,
            }
        return _refresh_capture_tool(db, tenant, args)

    if name == "billing_portal_link":
        # Do not invent Stripe; return instruction for UI to call existing endpoint
        return {
            "ui_fetch": {
                "method": "GET",
                "path": "/v1/account/billing-portal",
            },
            "note": "Client should open the returned portal URL. Energy Agent never charges cards.",
        }

    if name == "portal_links":
        # Vendor + utility portals for THIS tenant (Paul: SmartHub for Glover, etc.)
        PORTALS = {
            "solaredge": ("SolarEdge monitoring", "https://monitoring.solaredge.com/"),
            "fronius": ("Fronius Solar.web", "https://www.solarweb.com/"),
            "sma": ("SMA ennexOS / Sunny Portal", "https://ennexos.sunnyportal.com/"),
            "chint": ("Chint / CPS monitor", "https://monitor.chintpowersystems.com/"),
            "alsoenergy": ("AlsoEnergy", "https://hmi.alsoenergy.com/"),
            "locus": ("Locus / AlsoEnergy", "https://hmi.alsoenergy.com/"),
            "gmp": ("Green Mountain Power", "https://greenmountainpower.com/"),
            "vec": ("Vermont Electric Co-op (SmartHub)", "https://vermontelectric.smarthub.coop/"),
            "wec": ("Washington Electric Co-op (SmartHub)", "https://washingtonelectric.smarthub.coop/"),
        }
        try:
            from .models import Array, Inverter, UtilityAccount
            tid = tenant.id
            vendors = set()
            for v, in db.execute(
                select(Inverter.vendor).where(
                    Inverter.tenant_id == tid,
                    Inverter.deleted_at.is_(None),
                ).distinct()
            ).all():
                if v:
                    vendors.add(str(v).lower())
            # Arrays may exist without inverters yet (Paul's River / Norwich)
            for r in db.execute(
                select(Array.id, Array.name).where(
                    Array.tenant_id == tid, Array.deleted_at.is_(None),
                )
            ).all():
                pass  # presence only
            utils = []
            try:
                for ua in db.execute(
                    select(UtilityAccount).where(
                        UtilityAccount.tenant_id == tid,
                        UtilityAccount.deleted_at.is_(None),
                    )
                ).scalars().all():
                    code = (getattr(ua, "provider", None) or getattr(ua, "provider_code", None) or "").lower()
                    nick = getattr(ua, "nickname", None) or getattr(ua, "account_number", None) or code
                    utils.append({"provider": code, "label": nick})
                    if code:
                        vendors.add(code)
            except Exception:
                pass
            links = []
            for code in sorted(vendors):
                meta = PORTALS.get(code)
                if not meta:
                    # SmartHub co-ops often store host on the account
                    continue
                label, url = meta
                links.append({
                    "code": code,
                    "label": label,
                    "url": url,
                    "markdown": f"[{label}]({url})",
                })
            # Always include common VT utilities if tenant has VT offtakers / no utils yet
            if not any(L["code"] in ("vec", "wec", "gmp") for L in links):
                for code in ("vec", "gmp"):
                    label, url = PORTALS[code]
                    links.append({
                        "code": code,
                        "label": label + " (common VT)",
                        "url": url,
                        "markdown": f"[{label}]({url})",
                    })
            speak_lines = [
                f"- {L['markdown']}" for L in links
            ]
            return {
                "ok": True,
                "links": links,
                "utility_accounts": utils,
                "how_to_reply": (
                    "Embed these as markdown links in your spoken/chat answer, e.g. "
                    "'Open [VEC SmartHub](https://vermontelectric.smarthub.coop/) for Glover.' "
                    "You may also emit ui_command type open_url with url+label to open a tab."
                ),
                "chat_snippet": "Portal links:\n" + "\n".join(speak_lines),
            }
        except Exception as e:
            return {"error": str(e)}

    if name == "send_pipeline":
        return {
            "ui_fetch": {"method": "GET", "path": "/v1/array-operator/billing/send-pipeline"},
            "hint": "Prefer navigating user to #reports if they want to act.",
        }

    if name in ("ui_navigate", "ui_highlight", "ui_fill", "ui_click", "ui_tour", "open_url"):
        # Navigate + highlight + tours + open_url are instant. Writes skip confirm when the
        # user already stated the exact change this turn.
        if name in ("ui_navigate", "ui_highlight", "ui_tour", "open_url"):
            needs = False
        else:
            needs = bool(args.get("needs_confirm", True))
            if needs and _user_clearly_directed(user_text, args):
                needs = False
        # Walkthrough intent → PRESET tour only. Freehand ui_highlight invents CSS
        # and desyncs from voice; client has lockstep DOM tours for every tab.
        tour_id = _detect_tour_id(user_text) or (
            args.get("tour_id") if name == "ui_tour" else None
        )
        if name == "ui_highlight" and tour_id:
            name = "ui_tour"
            args = {"tour_id": tour_id}
        if name == "ui_tour":
            tid = args.get("tour_id") or tour_id
            if tid:
                args = {"tour_id": tid}  # drop freehand custom steps
        cmd_type = name.replace("ui_", "")
        if name == "ui_tour":
            cmd_type = "tour"
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": cmd_type,
            "args": {k: v for k, v in args.items() if k != "needs_confirm"},
            "needs_confirm": needs,
        }
        if needs:
            return {
                "status": "pending_confirm",
                "pending": cmd,
                "message": f"Ready to {cmd['type']}: {args.get('reason') or args.get('hash') or args.get('selector')}. Ask user to confirm.",
            }
        return {"status": "ui_command", "command": cmd}

    if name == "remember_tenant":
        _mem_set(db, f"tenant:{tid}", args.get("key") or "note", args.get("value") or "")
        return {"ok": True, "scope": "tenant"}

    if name == "remember_global_behavior":
        return _queue_global_memory(db, tid, args.get("key") or "tip", args.get("value") or "")

    if name == "escalate_to_ford":
        summary = args.get("summary") or "(no summary)"
        user_said = args.get("user_said") or ""
        quiet = bool(args.get("quietly"))
        # Durable Ford Operator inbox (standing Grok worker) — email is backup only.
        try:
            from .ford_escalations import enqueue_escalation
            email = (
                getattr(tenant, "contact_email", None)
                or getattr(tenant, "email", None)
                or ""
            )
            return enqueue_escalation(
                tenant_id=tid,
                tenant_email=str(email) if email else None,
                session_id=getattr(session, "id", None),
                summary=summary,
                user_said=user_said,
                quiet=quiet,
                also_email=not quiet,  # quiet auto-escapes: queue only, no mail spam
            )
        except Exception as e:
            log.exception("escalation queue failed — falling back to email")
            body = (
                f"Energy Agent escalation (queue failed: {e})\n"
                f"tenant: {tid}\n"
                f"email: {getattr(tenant, 'contact_email', '') or getattr(tenant, 'email', '')}\n"
                f"session: {session.id}\n"
                f"quiet: {quiet}\n\n"
                f"{summary}\n\n"
                f"User said:\n{user_said}\n"
            )
            try:
                send_internal_alert(f"[Energy Agent] {summary[:80]}", body)
                return {"ok": True, "escalated": True, "queue": "email_fallback"}
            except Exception as e2:
                log.exception("escalate email fallback failed")
                return {"ok": False, "error": str(e2)}

    if name == "patch_offtaker":
        # Resolve offtaker by id and/or name, map fields to real PATCH body,
        # optionally apply server-side so the UI can soft-refresh without a full reload.
        from .models import BillingReportSubscription

        sid = args.get("subscription_id")
        name_q = (args.get("offtaker_name") or args.get("customer_name") or "").strip()
        sub = None
        if sid is not None:
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return {"error": f"invalid subscription_id: {sid}"}
            sub = db.get(BillingReportSubscription, sid)
            if sub is None or sub.tenant_id != tid:
                return {"error": f"offtaker #{sid} not found in your account"}
            if getattr(sub, "deleted_at", None):
                return {"error": f"offtaker #{sid} is deleted"}
        elif name_q:
            listed = _run_tool("list_offtakers", {}, tenant, session, db)
            matches = [
                o for o in (listed.get("offtakers") or [])
                if name_q.lower() in str(o.get("name") or "").lower()
            ]
            if not matches:
                return {"error": f"no offtaker matching '{name_q}'", "hint": "call list_offtakers"}
            if len(matches) > 1:
                # Prefer exact match; otherwise ask which one
                exact = [o for o in matches if str(o.get("name") or "").lower() == name_q.lower()]
                if len(exact) == 1:
                    matches = exact
                else:
                    return {
                        "error": "multiple offtakers match — pass subscription_id",
                        "matches": [
                            {
                                "id": o.get("id"),
                                "name": o.get("name"),
                                "share_pct": o.get("share_pct"),
                                "array_name": o.get("array_name"),
                                "utility_label": o.get("utility_label"),
                            }
                            for o in matches[:12]
                        ],
                    }
            sid = matches[0]["id"]
            sub = db.get(BillingReportSubscription, sid)
        else:
            return {"error": "pass subscription_id or offtaker_name"}

        if sub is None:
            return {"error": "offtaker not found"}

        # Build API body with CORRECT field names (share_pct is NOT a column).
        payload: dict = {}
        if args.get("email") is not None:
            payload["client_email"] = str(args["email"]).strip()
        # Display rename ONLY when explicitly requested via name= — never treat
        # master_account / utility source targets as a customer_name change.
        if args.get("name") is not None:
            payload["customer_name"] = str(args["name"]).strip()
        if args.get("share_pct") is not None:
            try:
                sp = float(args["share_pct"])
            except (TypeError, ValueError):
                return {"error": "share_pct must be a number (e.g. 25 for 25%)"}
            # Accept either percent (25) or fraction (0.25)
            frac = sp / 100.0 if sp > 1.0 else sp
            if not (0 < frac <= 1.0):
                return {"error": "share_pct must be in (0, 100] percent or (0, 1] fraction"}
            # Sub-metered offtakers bill off their own meter (allocation_pct pinned 1.0);
            # their group share lives in array_share_pct. Mirror the Reports PATCH rule.
            has_own_meter = getattr(sub, "utility_account_id", None) is not None
            if has_own_meter:
                payload["array_share_pct"] = frac
            else:
                payload["allocation_pct"] = frac
        if args.get("auto_send") is not None:
            payload["delivery_mode"] = "auto" if bool(args["auto_send"]) else "approval"

        # ── Solar credit / net rate + discount ───────────────────────────
        try:
            if args.get("clear_rate"):
                payload["rate_per_kwh"] = None
                payload["net_rate_per_kwh"] = None
            else:
                if args.get("rate_per_kwh") is not None:
                    payload["rate_per_kwh"] = _validate_ea_rate(args["rate_per_kwh"])
                if args.get("net_rate_per_kwh") is not None:
                    payload["net_rate_per_kwh"] = _validate_ea_rate(args["net_rate_per_kwh"])
            if args.get("clear_discount"):
                payload["discount_pct"] = None
            elif args.get("discount_pct") is not None:
                payload["discount_pct"] = _validate_ea_discount(args["discount_pct"])
        except ValueError as e:
            return {"error": str(e)}

        # ── Utility / master group rebind ─────────────────────────────────
        bind = _resolve_offtaker_bind_targets(db, tid, sub, args)
        if bind.get("error"):
            return bind
        if "utility_account_id" in bind:
            payload["utility_account_id"] = bind["utility_account_id"]
        if "array_id" in bind:
            payload["array_id"] = bind["array_id"]

        if not payload:
            return {
                "error": (
                    "nothing to change — pass share_pct, email, name (rename only), "
                    "rate_per_kwh, net_rate_per_kwh, discount_pct, clear_rate, "
                    "auto_send, utility_account_id|utility_account_name, "
                    "array_id|array_name, and/or master_account"
                ),
                "offtaker": {
                    "id": sub.id,
                    "name": getattr(sub, "customer_name", None),
                    "email": getattr(sub, "client_email", None),
                    "array_id": getattr(sub, "array_id", None),
                    "utility_account_id": getattr(sub, "utility_account_id", None),
                    **_offtaker_rate_fields(db, tenant, sub),
                },
                "hint": (
                    "Solar credit rate = net_rate_per_kwh (or rate_per_kwh). "
                    "Call get_billing_rates(offtaker_name=...) to read current rates. "
                    "Master account / utility source = utility_account + array bind, "
                    "NOT customer_name."
                ),
            }

        needs = bool(args.get("needs_confirm", True))
        # Server-side gate: clear user direction → apply, never re-ask for "yes"
        if needs and _user_clearly_directed(user_text, payload):
            needs = False
        reason = (
            f"Update offtaker #{sub.id} ({getattr(sub, 'customer_name', '') or 'unnamed'}): "
            + ", ".join(f"{k}={v}" for k, v in payload.items())
        )
        if bind.get("resolved_labels"):
            reason += " (" + "; ".join(bind["resolved_labels"]) + ")"
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "api_patch",
            "args": {
                "method": "PATCH",
                "path": f"/v1/array-operator/billing/subscriptions/{sub.id}",
                "body": payload,
            },
            "needs_confirm": bool(needs),
            "reason": reason,
        }

        # When already confirmed (or model set needs_confirm=false), apply NOW
        # server-side so the change sticks even if the browser PATCH is flaky,
        # then tell the client to soft-refresh (no full page reload).
        if not needs:
            applied = _apply_offtaker_patch(db, sub, payload)
            if not applied.get("ok"):
                return applied
            refresh = {
                "id": uuid.uuid4().hex[:12],
                "type": "ui_refresh",
                "args": {
                    "surface": "reports",
                    "subscription_id": sub.id,
                    "allocation_pct": getattr(sub, "allocation_pct", None),
                    "array_share_pct": getattr(sub, "array_share_pct", None),
                    "array_id": getattr(sub, "array_id", None),
                    "utility_account_id": getattr(sub, "utility_account_id", None),
                    "customer_name": getattr(sub, "customer_name", None),
                    "client_email": getattr(sub, "client_email", None),
                    "rate_per_kwh": getattr(sub, "rate_per_kwh", None),
                    "net_rate_per_kwh": getattr(sub, "net_rate_per_kwh", None),
                    "discount_pct": getattr(sub, "discount_pct", None),
                },
                "needs_confirm": False,
            }
            applied["rates"] = _offtaker_rate_fields(db, tenant, sub)
            return {
                "status": "ui_command",
                "command": refresh,
                "also_commands": [cmd],  # client may still hit the path; idempotent
                "applied": applied,
                "message": f"Updated offtaker #{sub.id}. UI should soft-refresh Invoices.",
            }

        return {
            "status": "pending_confirm",
            "pending": cmd,
            "message": (
                f"Ready: {reason}. Only ask for confirm if the user's request was "
                "ambiguous — if they already stated the exact change, re-call with "
                "needs_confirm=false."
            ),
            "preview": {
                "subscription_id": sub.id,
                "name": getattr(sub, "customer_name", None),
                "payload": payload,
                "resolved": bind.get("resolved_labels"),
            },
        }

    return {"error": f"unknown tool {name}"}


def _resolve_offtaker_bind_targets(db, tid: str, sub, args: dict) -> dict:
    """Resolve array_id / utility_account_id from patch_offtaker args.

    Supports explicit ids, utility nickname/account #, array name, and the UI
    concept of 'master account' (group host) without renaming the offtaker.
    """
    from .models import UtilityAccount

    out: dict = {}
    labels: list[str] = []

    # Explicit ids win when provided
    explicit_ua = args.get("utility_account_id")
    explicit_arr = args.get("array_id")
    ua_name = (
        args.get("utility_account_name")
        or args.get("bill_source")
        or args.get("sub_account")
        or args.get("sub_account_name")
        or ""
    ).strip()
    arr_name = (args.get("array_name") or "").strip()
    master = (args.get("master_account") or args.get("master_account_name") or "").strip()

    # Resolve utility by name
    resolved_ua = None
    if explicit_ua is not None:
        try:
            uaid = int(explicit_ua)
        except (TypeError, ValueError):
            return {"error": f"invalid utility_account_id: {explicit_ua}"}
        resolved_ua = db.get(UtilityAccount, uaid)
        if (
            resolved_ua is None
            or resolved_ua.tenant_id != tid
            or getattr(resolved_ua, "deleted_at", None)
        ):
            return {"error": f"utility account #{uaid} not found in your account"}
    elif ua_name:
        resolved_ua, err = _find_utility_account(db, tid, ua_name)
        if err:
            return err
        if resolved_ua is None:
            return {
                "error": f"no utility account matching '{ua_name}'",
                "hint": "query_tenant resource=utility_accounts or check list_offtakers labels",
            }

    # Resolve array by id/name
    resolved_arr = None
    if explicit_arr is not None:
        try:
            aid = int(explicit_arr)
        except (TypeError, ValueError):
            return {"error": f"invalid array_id: {explicit_arr}"}
        resolved_arr = db.get(Array, aid)
        if (
            resolved_arr is None
            or resolved_arr.tenant_id != tid
            or getattr(resolved_arr, "deleted_at", None)
        ):
            return {"error": f"array #{aid} not found in your account"}
    elif arr_name:
        resolved_arr, err = _find_array(db, tid, arr_name)
        if err:
            return err
        if resolved_arr is None:
            return {"error": f"no array matching '{arr_name}'", "hint": "call tenant_census"}

    # master_account = UI master dropdown (group host). Prefer utility nickname,
    # then array name. Does NOT set customer_name.
    if master and resolved_ua is None and resolved_arr is None:
        ua_hit, _ = _find_utility_account(db, tid, master)
        arr_hit, arr_err = _find_array(db, tid, master)
        if ua_hit is not None and arr_hit is not None:
            # Prefer utility when nickname matches (dropdown labels are utility-based)
            resolved_ua = ua_hit
            # Also set master group array from the utility's array if present
            if ua_hit.array_id:
                resolved_arr = db.get(Array, ua_hit.array_id)
        elif ua_hit is not None:
            resolved_ua = ua_hit
            if ua_hit.array_id:
                resolved_arr = db.get(Array, ua_hit.array_id)
        elif arr_hit is not None:
            resolved_arr = arr_hit
            # Pick host utility for that array (lowest id among accounts on array)
            host = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tid,
                    UtilityAccount.array_id == arr_hit.id,
                    UtilityAccount.deleted_at.is_(None),
                ).order_by(UtilityAccount.id).limit(1)
            ).scalars().first()
            # For percent-of-master offtakers (no distinct sub), rebind host bill.
            # For offtakers already on their own sub-meter, only update array_id.
            cur_ua = getattr(sub, "utility_account_id", None)
            if host is not None:
                if cur_ua is None or cur_ua == host.id:
                    resolved_ua = host
                else:
                    # Keep their sub-meter; only move the master group
                    labels.append(
                        f"kept sub-meter utility_account_id={cur_ua}; "
                        f"master group → array {arr_hit.name}"
                    )
        else:
            return {
                "error": f"no master account / group matching '{master}'",
                "hint": (
                    "Master account labels are utility nicknames (e.g. Timberworks) "
                    "or array/group names. query_tenant resource=utility_accounts."
                ),
                **(arr_err or {}),
            }

    if resolved_ua is not None:
        out["utility_account_id"] = resolved_ua.id
        nick = (getattr(resolved_ua, "nickname", None) or "").strip()
        labels.append(
            f"utility_account_id={resolved_ua.id}"
            + (f" ({nick})" if nick else f" (acct {resolved_ua.account_number})")
        )
        # If caller didn't pick array, derive from utility (host group) — same as API
        if resolved_arr is None and resolved_ua.array_id and "array_id" not in out:
            # Only auto-fill array when master_account or ua was the primary bind
            if master or ua_name or explicit_ua is not None:
                if explicit_arr is None and not arr_name:
                    # Leave array to _apply / PATCH derivation unless master set it
                    pass

    if resolved_arr is not None:
        out["array_id"] = resolved_arr.id
        labels.append(f"array_id={resolved_arr.id} ({resolved_arr.name})")
    elif resolved_ua is not None and resolved_ua.array_id is not None:
        # Mirror routes.py: derive array_id from bound account when not explicit
        out["array_id"] = resolved_ua.array_id
        arr = db.get(Array, resolved_ua.array_id)
        labels.append(
            f"array_id={resolved_ua.array_id}"
            + (f" ({arr.name})" if arr else "")
            + " [from utility]"
        )

    if labels:
        out["resolved_labels"] = labels
    return out


def _find_array(db, tid: str, name_q: str) -> tuple:
    """Return (Array|None, error_dict|None). Partial name match, prefer exact."""
    q = (name_q or "").strip().lower()
    if not q:
        return None, None
    rows = db.execute(
        select(Array).where(
            Array.tenant_id == tid,
            Array.deleted_at.is_(None),
        )
    ).scalars().all()
    matches = [a for a in rows if q in (a.name or "").lower()]
    if not matches:
        return None, None
    exact = [a for a in matches if (a.name or "").lower() == q]
    if len(exact) == 1:
        return exact[0], None
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "error": f"multiple arrays match '{name_q}' — pass array_id",
        "matches": [{"id": a.id, "name": a.name} for a in matches[:12]],
    }


def _find_utility_account(db, tid: str, name_q: str) -> tuple:
    """Return (UtilityAccount|None, error_dict|None). Match nickname, acct #, address."""
    from .models import UtilityAccount

    q = (name_q or "").strip().lower()
    if not q:
        return None, None
    rows = db.execute(
        select(UtilityAccount).where(
            UtilityAccount.tenant_id == tid,
            UtilityAccount.deleted_at.is_(None),
        )
    ).scalars().all()

    def _addr(u) -> str:
        sa = getattr(u, "service_address", None)
        if isinstance(sa, dict):
            return " ".join(str(v) for v in sa.values() if v).lower()
        return str(sa or "").lower()

    def _score(u) -> int:
        nick = (getattr(u, "nickname", None) or "").strip().lower()
        acct = (getattr(u, "account_number", None) or "").strip().lower()
        addr = _addr(u)
        if nick == q or acct == q:
            return 3
        if nick and q in nick:
            return 2
        if acct and q in acct:
            return 2
        if addr and q in addr:
            return 1
        return 0

    scored = [(u, _score(u)) for u in rows]
    scored = [(u, s) for u, s in scored if s > 0]
    if not scored:
        return None, None
    scored.sort(key=lambda x: -x[1])
    best = scored[0][1]
    top = [u for u, s in scored if s == best]
    if len(top) == 1:
        return top[0], None
    # Prefer exact nickname among ties
    exact_nick = [
        u for u in top
        if (getattr(u, "nickname", None) or "").strip().lower() == q
    ]
    if len(exact_nick) == 1:
        return exact_nick[0], None
    return None, {
        "error": f"multiple utility accounts match '{name_q}' — pass utility_account_id",
        "matches": [
            {
                "id": u.id,
                "nickname": getattr(u, "nickname", None),
                "account_number": u.account_number,
                "provider": u.provider,
                "array_id": u.array_id,
            }
            for u in top[:12]
        ],
    }


def _apply_offtaker_patch(db, sub, payload: dict) -> dict:
    """Apply a validated offtaker field map to the ORM row and commit.

    Supports the same core fields as PATCH /billing/subscriptions/{id}:
    customer_name, client_email, allocation_pct, array_share_pct, delivery_mode,
    array_id, utility_account_id (+ sub-meter invariant).
    """
    try:
        from .models import UtilityAccount

        if "client_email" in payload:
            sub.client_email = payload["client_email"] or None
        if "customer_name" in payload:
            sub.customer_name = payload["customer_name"]
        if "allocation_pct" in payload:
            pct = float(payload["allocation_pct"])
            if not (0 < pct <= 1.0):
                return {"ok": False, "error": "allocation_pct must be fraction in (0, 1]"}
            sub.allocation_pct = pct
        if "array_share_pct" in payload:
            pct = float(payload["array_share_pct"])
            if not (0 < pct <= 1.0):
                return {"ok": False, "error": "array_share_pct must be fraction in (0, 1]"}
            sub.array_share_pct = pct
        if "delivery_mode" in payload:
            dm = payload["delivery_mode"]
            if dm not in ("approval", "auto"):
                return {"ok": False, "error": "delivery_mode must be approval or auto"}
            sub.delivery_mode = dm

        # Solar credit / net rates + discount (explicit None clears override)
        if "rate_per_kwh" in payload:
            sub.rate_per_kwh = payload["rate_per_kwh"]
        if "net_rate_per_kwh" in payload:
            sub.net_rate_per_kwh = payload["net_rate_per_kwh"]
        if "discount_pct" in payload:
            sub.discount_pct = payload["discount_pct"]

        # Array (master group) first so utility rebind can preserve explicit array
        if "array_id" in payload and payload["array_id"] is not None:
            aid = int(payload["array_id"])
            arr = db.get(Array, aid)
            if arr is None or arr.tenant_id != sub.tenant_id or getattr(arr, "deleted_at", None):
                return {"ok": False, "error": f"array #{aid} not found"}
            sub.array_id = aid

        if "utility_account_id" in payload and payload["utility_account_id"] is not None:
            uaid = int(payload["utility_account_id"])
            acct = db.get(UtilityAccount, uaid)
            if (
                acct is None
                or acct.tenant_id != sub.tenant_id
                or getattr(acct, "deleted_at", None)
            ):
                return {"ok": False, "error": f"utility account #{uaid} not found"}
            sub.utility_account_id = uaid
            # Derive array_id from utility only when caller did not set array explicitly
            # (master+sub: array_id = group host, utility = offtaker's own sub-meter)
            if "array_id" not in payload or payload.get("array_id") is None:
                if acct.array_id is not None:
                    sub.array_id = acct.array_id

        # Sub-meter invariant: own meter (≠ group host) → allocation_pct = 1.0
        if (
            "utility_account_id" in payload
            or "array_id" in payload
            or "allocation_pct" in payload
            or "array_share_pct" in payload
        ):
            if sub.utility_account_id is not None and sub.array_id is not None:
                host_id = db.execute(
                    select(UtilityAccount.id).where(
                        UtilityAccount.array_id == sub.array_id,
                        UtilityAccount.deleted_at.is_(None),
                    ).order_by(UtilityAccount.id)
                ).scalars().first()
                if host_id is not None and host_id != sub.utility_account_id:
                    # Route share to array_share_pct if only allocation was set
                    if (
                        "array_share_pct" not in payload
                        and "allocation_pct" in payload
                        and payload.get("allocation_pct") is not None
                    ):
                        sub.array_share_pct = float(payload["allocation_pct"])
                    sub.allocation_pct = 1.0

        db.add(sub)
        db.commit()
        db.refresh(sub)
        return {
            "ok": True,
            "subscription_id": sub.id,
            "customer_name": sub.customer_name,
            "client_email": sub.client_email,
            "allocation_pct": sub.allocation_pct,
            "array_share_pct": getattr(sub, "array_share_pct", None),
            "array_id": getattr(sub, "array_id", None),
            "utility_account_id": getattr(sub, "utility_account_id", None),
            "delivery_mode": getattr(sub, "delivery_mode", None),
            "rate_per_kwh": getattr(sub, "rate_per_kwh", None),
            "net_rate_per_kwh": getattr(sub, "net_rate_per_kwh", None),
            "discount_pct": getattr(sub, "discount_pct", None),
        }
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.exception("patch_offtaker apply failed")
        return {"ok": False, "error": str(e)}


# ── LLM ─────────────────────────────────────────────────────────────────────
def _http_json(url: str, headers: dict, body: dict | None = None, method: str = "POST", timeout: int = 90) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:800]
        raise HTTPException(502, f"Upstream {e.code}: {err}") from e


def _xai_ready() -> bool:
    if XAI_API_KEY:
        return True
    try:
        from .xai_auth import _oidc_config
        return bool(_oidc_config())
    except Exception:
        return False


def _call_grok(messages: list[dict], tools: list, *, max_tokens: int | None = None) -> dict:
    """OpenAI-compatible chat.completions via xAI. Returns message dict + usage.

    Bearer may be classic console.x.ai API key OR Grok Build OIDC (prepaid credits).
    """
    try:
        from .xai_auth import get_xai_bearer
        bearer = get_xai_bearer()
    except Exception as e:
        if not XAI_API_KEY:
            raise RuntimeError(f"no_xai: {e}") from e
        bearer = XAI_API_KEY
    tok = int(max_tokens or os.getenv("EA_MAX_TOKENS", "1200") or 1200)
    tok = max(256, min(tok, 4096))
    body = {
        "model": XAI_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.4,
        "max_tokens": tok,
    }
    out = _http_json(
        f"{XAI_BASE}/chat/completions",
        {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
        body,
        timeout=int(os.getenv("EA_GROK_TIMEOUT", "60") or 60),
    )
    choice = (out.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = out.get("usage") or {}
    return {"message": msg, "usage": usage, "provider": "xai"}


def _openai_content_to_anthropic(content: Any) -> Any:
    """Map OpenAI-style multimodal content (text + image_url) → Anthropic blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text") or ""})
        elif ptype == "image_url":
            url = ((part.get("image_url") or {}) if isinstance(part.get("image_url"), dict)
                   else {}).get("url") or ""
            if isinstance(part.get("image_url"), str):
                url = part.get("image_url") or ""
            if not url:
                continue
            # data:image/png;base64,....
            media = "image/png"
            b64 = ""
            if url.startswith("data:") and ";base64," in url:
                head, b64 = url.split(";base64,", 1)
                media = head.replace("data:", "").strip() or "image/png"
            else:
                # Non-data URLs — Claude needs base64; skip with a note
                blocks.append({
                    "type": "text",
                    "text": f"[Image URL not inlined: {url[:120]}]",
                })
                continue
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": b64,
                },
            })
        else:
            # pass through tool_result etc.
            blocks.append(part)
    return blocks if blocks else ""


def _call_anthropic(messages: list[dict], tools: list, *, max_tokens: int | None = None) -> dict:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("no_anthropic")
    # Convert tools to Anthropic shape
    a_tools = []
    for t in tools:
        fn = t.get("function") or {}
        a_tools.append({
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    sys = ""
    a_msgs = []
    for m in messages:
        if m["role"] == "system":
            sc = m.get("content") or ""
            if isinstance(sc, list):
                sc = " ".join(
                    (p.get("text") or "") for p in sc if isinstance(p, dict) and p.get("type") == "text"
                )
            sys += str(sc) + "\n"
        elif m["role"] == "tool":
            a_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "tool",
                    "content": m.get("content") or "",
                }],
            })
        elif m["role"] == "assistant" and m.get("tool_calls"):
            content = []
            if m.get("content"):
                c = m["content"]
                if isinstance(c, list):
                    content.extend(_openai_content_to_anthropic(c) or [])
                else:
                    content.append({"type": "text", "text": c})
            for tc in m["tool_calls"]:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"].get("arguments") or "{}"),
                })
            a_msgs.append({"role": "assistant", "content": content})
        else:
            raw = m.get("content")
            if isinstance(raw, list):
                a_msgs.append({
                    "role": m["role"],
                    "content": _openai_content_to_anthropic(raw),
                })
            else:
                a_msgs.append({"role": m["role"], "content": raw or ""})
    tok = int(max_tokens or os.getenv("EA_MAX_TOKENS", "1200") or 1200)
    tok = max(256, min(tok, 4096))
    body = {
        # claude-sonnet-4-20250514 is retired/404 on current API — use 4.5 alias.
        "model": os.getenv("EA_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        "max_tokens": tok,
        "system": sys or PERSONA,
        "messages": a_msgs,
        "tools": a_tools,
    }
    out = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
    )
    # Normalize to OpenAI-like message
    text_parts = []
    tool_calls = []
    for block in out.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts).strip()}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = {
        "prompt_tokens": (out.get("usage") or {}).get("input_tokens", 0),
        "completion_tokens": (out.get("usage") or {}).get("output_tokens", 0),
    }
    return {"message": msg, "usage": usage, "provider": "anthropic"}


_PRIMARY_DOWN_ALERT_EVERY_S = int(os.getenv("EA_PRIMARY_DOWN_ALERT_EVERY_S", "21600") or 21600)
_primary_down_alert_at: dict[str, float] = {}


def _alert_primary_llm_down(provider: str, err: str) -> None:
    """Loud, throttled (per-provider, default 6h) alert when the primary EA brain
    is failing and turns are silently riding the fallback. Never trade
    reliability for quiet — a degraded brain must be visible."""
    now = time.time()
    if now - _primary_down_alert_at.get(provider, 0.0) < _PRIMARY_DOWN_ALERT_EVERY_S:
        return
    _primary_down_alert_at[provider] = now
    log.error("EA primary LLM %s DOWN — running on fallback: %s", provider, err)
    try:
        from .notify import send_internal_alert
        send_internal_alert(
            f"[EnergyAgent] primary LLM '{provider}' DOWN — turns running on fallback",
            "Every Energy Agent turn is currently falling back to the secondary model.\n\n"
            f"provider: {provider}\nerror: {err}\n\n"
            "Fix the credential (Grok Build OIDC / API key) or flip "
            "ENERGY_AGENT_LLM_PRIMARY deliberately. This alert repeats every "
            f"{_PRIMARY_DOWN_ALERT_EVERY_S // 3600}h while the primary stays down.",
        )
    except Exception as e:
        log.error("primary-LLM-down alert email failed: %s", e)


def _call_llm(messages: list[dict], *, max_tokens: int | None = None) -> dict:
    """Grok (Build OIDC / API key) first when ready; Claude cloth fallback."""
    primary = (os.getenv("ENERGY_AGENT_LLM_PRIMARY") or "grok").strip().lower()
    order = []
    if primary in ("claude", "anthropic", "cloth"):
        order = ["claude", "grok"]
    else:
        # Default Grok-first — bills Ford's Grok Build credits when OIDC is wired
        order = ["grok", "claude"]
    last_err = None
    for who in order:
        is_primary = who == order[0]
        try:
            if who == "grok" and _xai_ready():
                return _call_grok(messages, TOOL_DEFS, max_tokens=max_tokens)
            if who == "claude" and ANTHROPIC_API_KEY:
                return _call_anthropic(messages, TOOL_DEFS, max_tokens=max_tokens)
            if is_primary:
                _alert_primary_llm_down(who, "not configured (missing key/credential)")
        except Exception as e:
            last_err = e
            if is_primary:
                _alert_primary_llm_down(who, repr(e))
            log.warning("%s failed, trying next LLM: %s", who, e)
            continue
    if last_err:
        log.warning("all LLMs failed, last=%s", last_err)
    # Offline stub — no LLM keys
    return {
        "message": {
            "role": "assistant",
            "content": (
                "I'm Energy Agent, but my reasoning keys aren't configured yet "
                "(set XAI_API_KEY or ANTHROPIC_API_KEY on the server). "
                "I can still take structured commands once tools are wired. "
                "Please escalate this setup gap to Ford."
            ),
            "tool_calls": [{
                "id": "esc_setup",
                "type": "function",
                "function": {
                    "name": "escalate_to_ford",
                    "arguments": json.dumps({
                        "summary": "Energy Agent LLM keys missing (XAI/ANTHROPIC)",
                        "user_said": messages[-1].get("content", "") if messages else "",
                        "quietly": True,
                    }),
                },
            }],
        },
        "usage": {},
        "provider": "stub",
    }


def _usage_cost(usage: dict) -> float:
    pin = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    pout = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return (pin / 1000.0) * COST_PER_1K_INPUT + (pout / 1000.0) * COST_PER_1K_OUTPUT


_VISUAL_FIX_RE = re.compile(
    r"\b(color|colour|look(s|ing)?|ugly|pretty|style|styling|theme|contrast|"
    r"button|chip|badge|doesn.?t look|does not look|fix the (color|colour|button)|"
    r"hard to (read|see|scan))\b",
    re.I,
)


def _visual_fix_fast_path(
    db, tenant: Tenant, session: EaSession, user_text: str, context: dict | None,
) -> dict | None:
    """Short ack + quiet propose_site_improvement — no design monologue, no tool spam."""
    ctx = context or {}
    text = (user_text or "").strip()
    if not text or len(text) < 8:
        return None
    force = bool(ctx.get("visual_fix_fast") or ctx.get("prefer_short_reply"))
    if not force and not _VISUAL_FIX_RE.search(text):
        return None
    # Don't steal pure data asks that merely mention "look"
    if re.search(r"\b(share|percent|kwh|invoice|offtaker|underperform|fault)\b", text, re.I) and not force:
        if not re.search(r"\b(button|color|colour|style|theme|ugly)\b", text, re.I):
            return None

    reply = (
        "Oh I see — I'll fix that. Working on it in the background; "
        "I'll nudge you when there's something to refresh and check."
    )
    tool_trace = []
    ui_commands = []
    # Queue improvement as this mind (not a separate "agent")
    try:
        out = _propose_site_improvement_tool(db, tenant, {
            "text": text[:2000],
            "force_submit": True,
            "start_markup": False,
        })
        tool_trace.append({
            "name": "propose_site_improvement",
            "args": {"text": text[:200], "force_submit": True},
            "result": {k: out.get(k) for k in ("ok", "suggestion_id", "status", "message", "error") if k in out},
        })
        if out.get("command"):
            ui_commands.append(out["command"])
        elif out.get("status") == "ui_command" and out.get("command"):
            ui_commands.append(out["command"])
    except Exception as e:
        log.warning("visual fix propose failed: %s", e)
        # Fall back to client markup command
        ui_commands.append({
            "id": uuid.uuid4().hex[:12],
            "type": "improve_site",
            "args": {"mark_first": False, "hint": text[:400]},
            "needs_confirm": False,
        })

    mind_out = None
    try:
        from .energy_agent_mind import classify_and_plan
        mind_plan = classify_and_plan(
            db, tenant.id, session.id, text, context=ctx,
        )
        if mind_plan:
            mind_out = {
                "plan_id": mind_plan.get("plan_id"),
                "intent": mind_plan.get("intent"),
                "task_count": len(mind_plan.get("tasks") or []),
                "note": "Quiet UX work — same mind.",
            }
    except Exception as e:
        log.warning("visual fix mind skipped: %s", e)

    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="user", content=text[:8000],
        meta_json=json.dumps({"context": ctx, "visual_fix_fast": True}) if ctx else None,
    ))
    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="assistant", content=reply,
        meta_json=json.dumps({"tool_trace": tool_trace, "ui_commands": ui_commands, "visual_fix_fast": True}),
    ))

    return {
        "reply": reply,
        "speak": _spoken_line(reply),
        "ui_commands": ui_commands,
        "pending": None,
        "tool_trace": tool_trace,
        "budget": _check_budget(db, tenant.id),
        "provider": "visual_fix_fast",
        "mind": mind_out,
        "cost_usd": 0.0,
    }


def _ea_extract_text(filename: str, mime: str, data: bytes) -> str:
    """Best-effort text extract for LLM context (no heavy PDF parsers required)."""
    name = (filename or "file").lower()
    ext = Path(name).suffix
    mime = (mime or "").lower()
    if not data:
        return ""
    if ext in _EA_TEXT_EXTS or mime.startswith("text/") or mime in (
        "application/json", "application/javascript", "application/xml",
        "application/x-yaml", "application/toml",
    ):
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return data.decode(enc)[:_EA_MAX_TEXT_EXTRACT]
            except Exception:
                continue
        return ""
    if ext == ".pdf" or "pdf" in mime:
        try:
            raw = data.decode("latin-1", errors="ignore")
            chunks = re.findall(r"\(([^)]{4,200})\)", raw)
            text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
            if len(text) > 80:
                return text[:_EA_MAX_TEXT_EXTRACT]
        except Exception:
            pass
        return f"[PDF binary: {filename}, {len(data)} bytes — no text extract]"
    if mime.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return f"[Image attachment: {filename}, {mime or 'image'}, {len(data)} bytes]"
    return f"[Binary attachment: {filename}, {mime or 'unknown'}, {len(data)} bytes]"


def _ea_serialize_asset(a: EaChatAsset) -> dict:
    return {
        "id": a.id,
        "filename": a.filename,
        "mime": a.mime,
        "size": a.size,
        "kind": a.kind,
        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        "preview": (a.text_extract or "")[:240],
        "has_text": bool((a.text_extract or "").strip()),
    }


def _ea_ensure_asset_table(db=None) -> None:
    try:
        if db is not None:
            bind = db.get_bind()
        else:
            from .db import engine
            bind = engine
        Base.metadata.create_all(bind=bind, tables=[EaChatAsset.__table__])
    except Exception:
        log.exception("ea_chat_assets table create failed")


def _ea_load_assets(db, tenant_id: str, asset_ids: list[str] | None) -> list[EaChatAsset]:
    if not asset_ids:
        return []
    ids = [str(i) for i in asset_ids if i][:12]
    if not ids:
        return []
    rows = db.execute(
        select(EaChatAsset).where(
            EaChatAsset.id.in_(ids),
            EaChatAsset.tenant_id == tenant_id,
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _ea_assets_text_block(assets: list[EaChatAsset]) -> str:
    if not assets:
        return ""
    parts = ["## Attachments the owner handed you (ground truth — analyze these)"]
    for a in assets:
        body = (a.text_extract or "").strip()
        if len(body) > 14000:
            body = body[:14000] + "\n…[truncated]"
        parts.append(
            f"### File: {a.filename} ({a.mime}, {a.size} bytes, id={a.id})\n"
            f"```\n{body or '(binary / image — see visual content if provided)'}\n```"
        )
    return "\n\n".join(parts)


def _ea_user_content_multimodal(
    user_text: str, assets: list[EaChatAsset],
) -> str | list[dict]:
    """Build OpenAI-compatible user content: text + optional image data URLs."""
    text_parts = [(user_text or "").strip()]
    attach_md = _ea_assets_text_block(assets)
    if attach_md:
        text_parts.append(attach_md)
    text = "\n\n".join(p for p in text_parts if p)[:12000]
    if not text:
        text = "Please analyze the attached file(s)."

    image_parts: list[dict] = []
    for a in assets:
        if (a.kind or "") != "image" and not (a.mime or "").startswith("image/"):
            continue
        path = a.storage_path
        if not path or not Path(path).is_file():
            continue
        try:
            raw = Path(path).read_bytes()
        except Exception:
            continue
        if not raw or len(raw) > _EA_MAX_IMAGE_B64:
            # Too large for multimodal — text note already covers it
            continue
        mime = (a.mime or "image/png").split(";")[0].strip() or "image/png"
        if mime not in ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"):
            mime = "image/png"
        if mime == "image/jpg":
            mime = "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    if not image_parts:
        return text
    return [{"type": "text", "text": text}] + image_parts


def _is_voice_turn(source: str | None, context: dict | None) -> bool:
    """True when this chat turn should prefer short spoken-style replies."""
    src = (source or "").strip().lower()
    if src == "voice":
        return True
    ctx = context or {}
    if ctx.get("voice_source") is True or str(ctx.get("voice_source") or "").lower() in (
        "1", "true", "yes", "voice",
    ):
        return True
    if str(ctx.get("channel") or "").lower() == "voice":
        return True
    return False


def _mouth_line(reply: str, *, voice: bool = False) -> str:
    """Spoken line for the Realtime mouth — leaner than the chat bubble when needed.

    Voice turns get a hard word/sentence cap so long LLM dumps cannot start a
    monologue that barges/interrupts itself mid-stream.
    """
    text = (reply or "").strip()
    if not text:
        return ""
    # Strip markdown noise the mouth shouldn't vocalize
    plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    plain = re.sub(r"`([^`]+)`", r"\1", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
    plain = re.sub(r"^#+\s*", "", plain, flags=re.M)
    plain = re.sub(r"^[-*]\s+", "", plain, flags=re.M)
    plain = re.sub(r"^\d+\.\s+", "", plain, flags=re.M)
    plain = re.sub(r"\n{2,}", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not voice:
        # Chat path still prefers full speak when short; cap extreme dumps
        words = plain.split()
        if len(words) <= 90:
            return plain
        # First two sentences max for overlong text replies
        parts = re.split(r"(?<=[.!?])\s+", plain)
        short = " ".join(parts[:2]).strip() if parts else plain
        w = short.split()
        if len(w) > 90:
            short = " ".join(w[:90]).rstrip(",;:") + "."
        return short or plain

    # Voice: ~45 words / 2 sentences
    max_words = int(os.getenv("EA_VOICE_MAX_WORDS", "48") or 48)
    max_words = max(20, min(max_words, 80))
    parts = re.split(r"(?<=[.!?])\s+", plain)
    out = []
    count = 0
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        wc = len(p.split())
        if out and count + wc > max_words:
            break
        out.append(p)
        count += wc
        if len(out) >= 2 and count >= 18:
            break
        if count >= max_words:
            break
    spoken = " ".join(out).strip() if out else plain
    w = spoken.split()
    if len(w) > max_words:
        spoken = " ".join(w[:max_words]).rstrip(",;:") + "."
    return spoken or plain


# ── hybrid voice+text ───────────────────────────────────────────────────────
# The panel is a NARROW chat column; the voice is a spoken assistant. A single
# raw markdown blob served to both channels is what made replies feel like an
# overwhelming wall on screen AND a cut-off list fragment out loud. So we split:
#   TEXT  -> _tidy_chat_text(): panel-clean (no heading walls, no bold-everywhere)
#   VOICE -> _make_spoken(): a real one-liner a person would say, never a list slice

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
_PSEUDO_HEADER_RE = re.compile(
    r"^\s*(?:\*\*)?(top issues?|next steps?|here'?s|details?|breakdown|the rundown|"
    r"issues?|problems?|summary)\b.{0,40}:?\s*(?:\*\*)?\s*$",
    re.I,
)


def _tidy_chat_text(text: str) -> str:
    """Render clean in the narrow panel. Conservative: keeps every word, list,
    link and one emphasis — only removes the visual SHOUTING (markdown heading
    walls, '**Header:**' scaffolding lines, and bold-on-everything)."""
    t = (text or "").strip()
    if not t:
        return t
    # Protect fenced code blocks from tidying.
    blocks: list[str] = []

    def _stash(m):
        blocks.append(m.group(0))
        return f"\x00CB{len(blocks) - 1}\x00"

    t = re.sub(r"```[\s\S]*?```", _stash, t)

    out_lines = []
    for ln in t.split("\n"):
        s = ln.rstrip()
        # Markdown heading -> plain line (the panel renders '#'/'##' as big headers).
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)
        # A line that is ENTIRELY one bold span (a pseudo-header) -> de-bold it.
        m = re.match(r"^\s*\*\*(.+?)\*\*\s*(:?)\s*$", s)
        if m:
            s = (m.group(1).rstrip() + (m.group(2) or "")).rstrip()
        out_lines.append(s)
    t = "\n".join(out_lines)

    # Cap emphasis: keep the first TWO bold spans (the lead's key number/verdict),
    # unbold the rest so a bolded list of names/metrics stops reading as a wall.
    spans = list(_BOLD_RE.finditer(t))
    if len(spans) > 2:
        cut = spans[1].end()
        t = t[:cut] + _BOLD_RE.sub(lambda mm: mm.group(1), t[cut:])

    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    t = re.sub(r"\x00CB(\d+)\x00", lambda mm: blocks[int(mm.group(1))], t)
    return t


def _plain_for_speech(text: str) -> str:
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", text or "")
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)
    return re.sub(r"\s+", " ", s).strip()


def _spoken_line(reply: str) -> str:
    """Deterministic spoken lead: the answer BEFORE the first list/heading,
    cleaned, kept to WHOLE sentences (never cut mid-sentence), with a natural
    spoken offer when detail was left on screen. Never reads a list item aloud."""
    text = (reply or "").strip()
    if not text:
        return ""
    lead_lines, dropped = [], False
    for ln in text.split("\n"):
        st = ln.strip()
        if _LIST_LINE_RE.match(st) or re.match(r"^#{1,6}\s", st) or _PSEUDO_HEADER_RE.match(st):
            dropped = True
            break
        lead_lines.append(ln)
    lead = _plain_for_speech(" ".join(l.strip() for l in lead_lines if l.strip()))
    if not lead:
        # Whole reply was a list — speak the gist, not the items.
        lead = _plain_for_speech(text)
        dropped = True
    # Keep COMPLETE sentences up to a generous cap — never slice mid-sentence
    # (that was the "stops halfway through" cutoff). She should finish her thought.
    cap = max(40, min(int(os.getenv("EA_VOICE_LEAD_MAX_WORDS", "90") or 90), 200))
    parts = [p for p in re.split(r"(?<=[.!?])\s+", lead) if p.strip()]
    out, count = [], 0
    for p in parts:
        wc = len(p.split())
        if out and count + wc > cap:
            break
        out.append(p.strip())
        count += wc
    spoken = " ".join(out).strip() or lead
    if dropped and spoken and not spoken.rstrip().endswith(("?", "…")):
        spoken = spoken.rstrip() + " Want me to run through them?"
    return spoken or _plain_for_speech(text)


# Option B (Ford 2026-07-16): the ONE brain authors its own spoken line in the
# same reasoning pass — no dumber summarizer between the mind and its mouth. On
# voice-active turns the model ends its reply with a "[SPOKEN] …" line: the text
# above is shown on the panel, the [SPOKEN] line is what the voice speaks. Same
# mind, so the spoken words carry the tools it just called and its actual plan.
_SPOKEN_MARKER_RE = re.compile(r"(?is)\n?[ \t]*\[\s*spoken\s*\][ \t:]*")


def _split_spoken(text: str) -> tuple[str, str | None]:
    """Return (panel_text, spoken_line|None). Splits on the LAST [SPOKEN] marker
    the brain authored. Falls back to (text, None) when the model didn't emit one
    — the caller then speaks the brain's own lead sentence (still no Haiku)."""
    t = text or ""
    matches = list(_SPOKEN_MARKER_RE.finditer(t))
    if not matches:
        return t.strip(), None
    m = matches[-1]
    panel = t[: m.start()].strip()
    spoken = _plain_for_speech(t[m.end():])
    # Strip any stray earlier markers from the panel text (defensive).
    panel = _SPOKEN_MARKER_RE.sub(" ", panel).strip()
    if not spoken:
        return panel or t.strip(), None
    # If the model put everything after the marker (no panel text), show it too.
    return (panel or spoken), spoken


def _make_spoken(reply: str, *, voice_active: bool) -> str:
    """Fallback spoken line when the brain didn't author a [SPOKEN] line: its own
    lead sentence (never a Haiku re-summary)."""
    return _spoken_line(reply)


# ── live "thinking out loud" narration (Ford 2026-07-16) ──────────────────────
# Map the deep brain's REAL tool calls → short first-person phrases the voice
# speaks while it works, so the wait is filled with genuine, grounded thinking
# (never invented — each phrase is derived from a tool that's actually running).
_NAV_TAB_NAMES = {
    "#dashboard": "Fleet Triage", "#arrays": "Inverters", "#analysis": "Analysis",
    "#trends": "Trends", "#resources": "Resources", "#reports": "Invoices",
    "#ops": "Repairs", "#account": "Account",
}


def _narrate_tool(name: str, args: dict) -> str | None:
    n = (name or "").strip()
    a = args or {}
    if n in ("tenant_census",):
        return "Let me get a read on your whole fleet."
    if n in ("fleet_overview",):
        return "Checking how your arrays are doing right now."
    if n in ("investigate_attention",):
        return "Let me see what needs attention across the fleet."
    if n in ("array_detail",):
        nm = (a.get("array_name") or a.get("name") or "").strip()
        return f"Taking a closer look at {nm}." if nm else "Taking a closer look at that site."
    if n in ("production_forecast",):
        return "Comparing your output against what the weather should've given you."
    if n in ("query_tenant",):
        return "Digging into your numbers."
    if n in ("fleet_trends_summary",):
        return "Looking at your production trend."
    if n in ("list_recent_invoices",):
        return "Pulling up your recent invoices."
    if n in ("get_billing_rates", "get_offtaker", "list_offtakers"):
        return "Checking your offtaker rates."
    if n in ("account_summary",):
        return "Checking your account."
    if n in ("setup_status",):
        return "Let me see where your setup stands."
    if n in ("refresh_capture",):
        return "Kicking off a fresh data pull."
    if n in ("repair_ops_overview", "list_service_contacts", "list_repair_tickets"):
        return "Checking on your repairs."
    if n in ("get_billing_rates",):
        return "Checking your rates."
    if n in ("product_map",):
        topic = str(a.get("topic") or "").lower()
        if topic.startswith("surface_"):
            tab = topic.replace("surface_", "").replace("_", " ").title()
            return f"Getting the {tab} layout straight so I can walk you through it."
        return "Let me get the details right."
    if n in ("ui_navigate",):
        h = str(a.get("hash") or a.get("tab") or "").lower()
        if not h.startswith("#"):
            h = "#" + h
        tab = _NAV_TAB_NAMES.get(h)
        return f"Opening {tab} for you." if tab else None
    if n in ("ui_tour",):
        return "Let me set up the walkthrough."
    if n in ("web_search", "web_fetch"):
        return "Looking that up."
    # Granular / internal tools (ui_highlight, memory, escalate, confirm…): silent.
    return None


def _agent_turn(
    db,
    tenant: Tenant,
    session: EaSession,
    user_text: str,
    context: dict | None,
    attachment_ids: list[str] | None = None,
    source: str = "text",
    on_event=None,
) -> dict:
    budget = _check_budget(db, tenant.id)
    if not budget["ok"]:
        return {
            "reply": (
                f"You've used this week's Energy Agent allowance "
                f"(${WEEKLY_BUDGET_USD:.0f} for thinking + voice). "
                "It resets next week — I can still show what's already on screen, "
                "or Ford can raise the cap."
            ),
            "ui_commands": [],
            "pending": None,
            "tool_trace": [],
            "budget": budget,
            "provider": None,
            "mind": None,
        }

    assets = _ea_load_assets(db, tenant.id, attachment_ids)
    # Skip visual-fast path when attachments need real analysis
    if not assets:
        # Visual polish: short ack + quiet pipeline (skip multi-tool design lectures)
        try:
            fast = _visual_fix_fast_path(db, tenant, session, user_text, context)
            if fast is not None:
                return fast
        except Exception as e:
            log.warning("visual_fix_fast_path failed: %s", e)

    # Operating mind: plan only here (cheap). Do NOT drain heavy tasks on the
    # chat critical path — scheduler + client mind/tick handle that (speed).
    mind_plan = None
    try:
        from .energy_agent_mind import classify_and_plan, _world_get
        mind_plan = classify_and_plan(
            db, tenant.id, session.id, user_text, context=context or {},
        )
    except Exception as e:
        log.warning("mind plan skipped: %s", e)

    t_mem = _mem_get(db, f"tenant:{tenant.id}", 20)
    g_mem = _mem_get(db, "global", 12)
    # Include recent email-mirrored turns in hist (continuous surface)
    hist = db.execute(
        select(EaMessage).where(
            EaMessage.session_id == session.id,
            EaMessage.role.in_(("user", "assistant")),
        ).order_by(EaMessage.id.desc()).limit(18)
    ).scalars().all()
    hist = list(reversed(hist))

    system = PERSONA + "\n\nTenant memory:\n" + json.dumps(t_mem)[:2000]
    system += "\n\nGlobal behavior tips:\n" + json.dumps(g_mem)[:1200]
    # Fleet clock every turn — without this she invents night-time outages.
    try:
        _clock = _fleet_clock_context()
        system += (
            "\n\nFLEET CLOCK (GROUND TRUTH — array local time / sun-up; trust this over "
            "your own sense of 'now'):\n"
            + json.dumps(_clock, default=str)
        )
        if _clock.get("solar_state") == "night":
            system += (
                "\nIt is NIGHT at the fleet. Zero live power is normal. Do not say "
                "arrays are down/dead/offline because they aren't producing right now."
            )
    except Exception as _ce:
        log.info("fleet clock inject skipped: %s", _ce)
    if context:
        # Prefer fleet_attention_snapshot (client live view) — keep it intact
        snap = None
        try:
            snap = (context or {}).get("fleet_attention_snapshot")
        except Exception:
            snap = None
        ctx_for_prompt = dict(context) if isinstance(context, dict) else {}
        if snap is not None:
            # Put snapshot outside the truncated dump so it never gets cut
            ctx_for_prompt = {k: v for k, v in ctx_for_prompt.items() if k != "fleet_attention_snapshot"}
        system += "\n\nUI context:\n" + json.dumps(ctx_for_prompt, default=str)[:2000]
        if snap is not None:
            system += (
                "\n\nFLEET ATTENTION SNAPSHOT (GROUND TRUTH — same live view as Spreadsheet "
                "NEED ATTENTION; 14-day health + live dark/low). "
                "If this disagrees with a vague memory, TRUST THIS. "
                "Never say a listed site is healthy.\n"
                + json.dumps(snap, default=str)[:4500]
            )
    # Ground truth: repair email thread (same mind as this chat)
    email_digest = repair_email_surface_digest(db, tenant.id, limit=18)
    if email_digest:
        system += (
            "\n\nRecent repair email thread (GROUND TRUTH — same conversation as this chat; "
            "do not claim silence if inbound exists):\n"
            + email_digest[:3500]
        )
    # Standing objective: where this operator stands on being fully operational.
    # She always knows the gap and can lead with it (never asks "are you set up?").
    try:
        _setup = _compute_setup_status(db, tenant)
        if _setup.get("fully_operational") and not _setup.get("optional_gaps"):
            system += (
                "\n\nOBJECTIVE STATE: this operator is FULLY OPERATIONAL and data is fresh. "
                "Do not invent setup work or nag — just help with what they ask."
            )
        else:
            system += (
                "\n\nOBJECTIVE STATE (your standing job — get them fully operational, then keep "
                "them there): " + _setup.get("summary_line", "")
                + " Lead with this gap ONLY when it's relevant to the turn; name the specific "
                "gap and offer to act (call setup_status for detail, refresh_capture if data is "
                "stale). Never ask 'is everything set up?'."
            )
    except Exception as _e:
        log.info("objective-state inject skipped: %s", _e)
    system += (
        "\n\nSPEED: Prefer ONE tool call then answer. For solar credit / offtaker "
        "rates call get_billing_rates (or get_offtaker) first — do not call "
        "product_map for rate questions. Avoid multi-tool fishing."
    )
    voice_turn = _is_voice_turn(source, context)
    voice_active = voice_turn or bool((context or {}).get("voice_active"))
    if (source or "") == "voice_consult" or bool((context or {}).get("voice_weave")):
        # Deep brain for Option D — Realtime only speaks what you return.
        system += (
            "\n\nVOICE CONSULT (you ARE the deep brain for live voice): The weak Realtime "
            "model called you because it must not invent product UI or numbers. "
            "For tab walkthroughs / 'how does X work' / 'show me Analysis': ALWAYS call "
            "product_map(topic=surface_<tab> or surface) FIRST, then ui_navigate / "
            "ui_highlight as needed. Use exact top-nav labels (Fleet Triage, Inverters, "
            "Analysis, Invoices, Repairs, Account). Never invent buttons or steps that "
            "are not in the product map. Be concrete and sequential."
        )
    if voice_active:
        # ONE mind, two outputs it authors itself (Ford 2026-07-16, Option B):
        # the panel text (full) + a [SPOKEN] line YOU speak aloud. Same reasoning
        # pass, so the spoken words carry the tools you just called and your plan.
        system += (
            "\n\nYOU WILL BE HEARD (voice is live). Produce your answer in TWO parts:\n"
            "1) Your normal text answer for the on-screen panel — clear and complete "
            "(the owner may be reading it).\n"
            "2) Then a final line that starts EXACTLY with `[SPOKEN]` — the words your "
            "voice actually says out loud. Everything above `[SPOKEN]` is shown on screen; "
            "the `[SPOKEN]` line is NOT shown, only spoken.\n"
            "The [SPOKEN] line: human and complete — your answer plus where you're headed — "
            "said like a sharp colleague, not read like a report. You KNOW what you just "
            "did (the tools you called, your plan) — let that intelligence show. "
            "Usually 2–4 full sentences (enough to finish the thought out loud); go longer "
            "only if it genuinely needs it. FINISH every thought — never trail off mid-idea. "
            "Offer to go deeper if there's more. Put NO markdown / bullets / headings / '#' "
            "in it. If your whole answer is one short line, the [SPOKEN] line can just be "
            "that line, phrased for the ear."
        )
    else:
        system += (
            "\n\nBREVITY REMINDER: Prefer 1–3 short sentences or a tight list "
            "(≤4 bullets). Don't monologue. Offer more if needed."
        )
    if mind_plan:
        system += (
            "\n\nMind background (internal — do not dump task IDs to the user):\n"
            + json.dumps(mind_plan)[:1500]
        )
        system += (
            "\nIf mind_plan is set, prefer refining understanding in conversation "
            "while work continues. Do NOT say theater lines like 'looking into it' — "
            "only speak concrete progress or wait silently."
        )
    try:
        from .energy_agent_mind import _world_get as _wg
        world = _wg(db, tenant.id)
        if world.get("fleet_digest") or world.get("last_intent"):
            system += "\n\nWorld model digests:\n" + json.dumps({
                "last_intent": world.get("last_intent"),
                "fleet_digest": world.get("fleet_digest"),
            }, default=str)[:1500]
    except Exception:
        pass

    if assets:
        system += (
            "\n\nThe owner attached file(s)/image(s) this turn. Analyze them carefully: "
            "describe what you see, extract numbers/tables, and answer their question. "
            "Do not claim you cannot view images if image content is provided."
        )

    user_content = _ea_user_content_multimodal(user_text, assets)
    # Stored transcript is always plain text (images referenced by name)
    store_user = (user_text or "").strip()
    if assets:
        names = ", ".join(a.filename for a in assets)
        if store_user:
            store_user = f"{store_user}\n\n[Attached: {names}]"
        else:
            store_user = f"[Attached: {names}] — please analyze."

    messages: list[dict] = [{"role": "system", "content": system}]
    for m in hist:
        messages.append({"role": m.role, "content": (m.content or "")[:1800]})
    messages.append({"role": "user", "content": user_content})

    tool_trace = []
    ui_commands = []
    pending = None
    total_cost = 0.0
    provider = None
    final_text = ""
    # Voice must be able to FINISH its thought and hold a real conversation —
    # a tight token cap was clipping spoken answers mid-sentence (Ford 2026-07-16).
    try:
        default_max = int(os.getenv("EA_MAX_TOKENS", "1200") or 1200)
    except (TypeError, ValueError):
        default_max = 1200
    try:
        voice_max = int(os.getenv("EA_VOICE_MAX_TOKENS", "1500") or 1500)
    except (TypeError, ValueError):
        voice_max = 1500
    # Voice-active turns author BOTH the panel text and the [SPOKEN] line, so
    # give them the larger budget (not just source=voice turns).
    llm_max_tokens = voice_max if voice_active else default_max

    for _round in range(MAX_TOOL_ROUNDS):
        # Release the pooled DB connection before the (long, blocking) LLM HTTP
        # call — holding a txn across vendor HTTP is the documented whole-API
        # meltdown class. Tools re-acquire a connection lazily afterwards.
        try:
            db.commit()
        except Exception:
            db.rollback()
        result = _call_llm(messages, max_tokens=llm_max_tokens)
        provider = result.get("provider")
        total_cost += _usage_cost(result.get("usage") or {})
        msg = result["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            final_text = (msg.get("content") or "").strip()
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            tname = fn.get("name") or ""
            try:
                targs = json.loads(fn.get("arguments") or "{}")
            except Exception:
                targs = {}
            # Live "thinking out loud" — narrate what we're about to do BEFORE it
            # runs, so the voice can speak it in real time (grounded in the real
            # tool call, so it can't invent). Callback must never break the turn.
            if on_event is not None:
                phrase = _narrate_tool(tname, targs)
                if phrase:
                    try:
                        on_event({"type": "thinking", "text": phrase, "tool": tname})
                    except Exception:
                        pass
            out = _run_tool(tname, targs, tenant, session, db, user_text=user_text)
            tool_trace.append({"name": tname, "args": targs, "result": out})

            if isinstance(out, dict) and out.get("status") == "pending_confirm":
                pend = out.get("pending") or {}
                # Second chance: if the model left needs_confirm on but the user
                # already directed the write, apply now instead of Yes/No UI.
                body_preview = (pend.get("args") or {}).get("body") or (pend.get("args") or {})
                retriable = pend.get("type") == "api_patch" or bool(pend.get("tool"))
                if retriable and _user_clearly_directed(user_text, body_preview):
                    targs2 = dict(targs)
                    targs2["needs_confirm"] = False
                    out2 = _run_tool(tname, targs2, tenant, session, db, user_text=user_text)
                    tool_trace.append({"name": tname, "args": targs2, "result": out2})
                    out = out2
                    pending = None
                    session.pending_json = None
                else:
                    pending = pend
                    session.pending_json = json.dumps(pending)
            if isinstance(out, dict) and out.get("status") == "ui_command":
                ui_commands.append(out["command"])
                for extra in out.get("also_commands") or []:
                    ui_commands.append(extra)
                # Clear stale pending when we successfully applied a write
                if out.get("applied"):
                    pending = None
                    session.pending_json = None

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or tname,
                "content": json.dumps(out)[:8000],
            })
        else:
            continue
    else:
        final_text = final_text or "I hit my tool-step limit — tell me the next single step you want."

    if not final_text:
        final_text = (msg.get("content") or "").strip() or (
            "Done — check the tool timeline. Confirm if I'm waiting on a yes."
            if pending else "Done."
        )

    # Collapse freehand multi-highlight "tours" into one preset (client has real DOM)
    hl_n = sum(
        1
        for c in ui_commands
        if isinstance(c, dict) and str(c.get("type") or "") in ("highlight", "ui_highlight")
    )
    forced_tid = _detect_tour_id(user_text)
    if forced_tid and (hl_n >= 1 or any(
        isinstance(c, dict) and str(c.get("type") or "") in ("tour", "walkthrough")
        for c in ui_commands
    )):
        ui_commands = [{
            "id": uuid.uuid4().hex[:12],
            "type": "tour",
            "args": {"tour_id": forced_tid},
            "needs_confirm": False,
        }]
    elif hl_n >= 2:
        # No named tab, but model still freehanded a multi-step tour — drop highlights
        ui_commands = [
            c for c in ui_commands
            if not (isinstance(c, dict) and str(c.get("type") or "") in ("highlight", "ui_highlight"))
        ]

    # If we applied a write this turn, never leave a "say yes" speech bubble
    if any(
        isinstance(t.get("result"), dict) and t["result"].get("applied")
        for t in tool_trace
    ):
        pending = None
        session.pending_json = None
        if re.search(r"\b(say\s+yes|confirm|go\s+ahead|do\s+it)\b", final_text or "", re.I):
            final_text = "Done — I applied that change. The Invoices view should update without a refresh."

    # If any tool failed open-ended and model didn't escalate, still escalate quietly
    if any(
        isinstance(t.get("result"), dict) and t["result"].get("error")
        for t in tool_trace
    ) and not any(t.get("name") == "escalate_to_ford" for t in tool_trace):
        try:
            _run_tool(
                "escalate_to_ford",
                {
                    "summary": f"Tool error during session: {user_text[:120]}",
                    "user_said": user_text[:500],
                    "quietly": True,
                },
                tenant, session, db,
            )
            tool_trace.append({"name": "escalate_to_ford", "args": {"quietly": True}, "result": {"ok": True}})
        except Exception:
            pass

    _charge(db, tenant.id, total_cost, f"chat:{provider or 'none'}")
    session.cost_usd = float(session.cost_usd or 0) + total_cost

    # Option B: split the brain's authored [SPOKEN] line off the panel text FIRST
    # (it was written in the same pass, tool-aware), then panel-clean what's shown.
    _panel_text, _authored_spoken = _split_spoken(final_text)
    final_text = _tidy_chat_text(_panel_text)

    user_meta: dict[str, Any] = {}
    if context:
        user_meta["context"] = context
    if assets:
        user_meta["attachment_ids"] = [a.id for a in assets]
        user_meta["attachments"] = [
            {"id": a.id, "filename": a.filename, "mime": a.mime, "kind": a.kind}
            for a in assets
        ]
    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="user",
        content=(store_user or user_text)[:8000],
        meta_json=json.dumps(user_meta, default=str) if user_meta else None,
    ))
    db.add(EaMessage(
        session_id=session.id, tenant_id=tenant.id, role="assistant", content=final_text[:8000],
        meta_json=json.dumps({"tool_trace": tool_trace, "ui_commands": ui_commands, "pending": pending}),
    ))

    mind_out = None
    if mind_plan:
        mind_out = {
            "plan_id": mind_plan.get("plan_id"),
            "intent": mind_plan.get("intent"),
            "task_count": len(mind_plan.get("tasks") or []),
            "voice_steer": mind_plan.get("voice_steer"),
            "note": (
                "Background cognition running — same mind. "
                "Voice is the mouth; this mind steers what it says."
            ),
        }

    # The brain's own authored spoken line (same mind, tool-aware) when present;
    # else its own lead sentence — never a separate summarizer.
    spoken = _authored_spoken or _make_spoken(final_text, voice_active=voice_active)
    return {
        "reply": final_text,
        # Mouth speaks this — authored by the SAME brain that just reasoned and
        # called tools, so the spoken words carry its plan (no Haiku middleman).
        "speak": spoken,
        "ui_commands": ui_commands,
        "pending": pending,
        "tool_trace": tool_trace,
        "budget": _check_budget(db, tenant.id),
        "provider": provider,
        "mind": mind_out,
        "cost_usd": round(total_cost, 6),
        "voice_turn": voice_turn,
    }


# ── routes ──────────────────────────────────────────────────────────────────
@router.post("/v1/energy-agent/session")
def create_session(body: SessionIn, authorization: str | None = Header(default=None)):
    """Start or resume a conversation.

    Default resume=True: reattach the tenant's open session + message history.
    Chat history and operating mind (world model / tasks) live in the DB, so a
    browser refresh or localStorage clear does not wipe the mind — only sign-out
    or force_new starts clean.
    """
    t = _auth(authorization)
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        ctx = body.context or {}
        brain = "grok" if XAI_API_KEY else ("claude" if ANTHROPIC_API_KEY else "stub")
        realtime_ready = bool(OPENAI_API_KEY)

        # ── Resume existing open conversation (survives refresh / cache clear) ──
        if body.resume and not body.force_new:
            existing = _find_resumable_session(
                db, t.id, preferred_id=body.preferred_session_id,
            )
            if existing is not None:
                if ctx:
                    # Merge fresh UI context; keep prior keys as fallback
                    try:
                        prev = json.loads(existing.context_json or "{}")
                    except Exception:
                        prev = {}
                    if not isinstance(prev, dict):
                        prev = {}
                    prev.update(ctx)
                    existing.context_json = json.dumps(prev)[:8000]
                messages = _session_messages_payload(db, existing.id)
                db.commit()
                return {
                    "ok": True,
                    "session_id": existing.id,
                    "resumed": True,
                    "messages": messages,
                    "message_count": len(messages),
                    "budget": budget,
                    # No full intro on resume — client paints history instead
                    "intro": None,
                    "welcome_back": (
                        "Still here — picking up where we left off."
                        if messages
                        else None
                    ),
                    "realtime_ready": realtime_ready,
                    "brain": brain,
                }

        sid = "ea_" + uuid.uuid4().hex[:16]
        s = EaSession(
            id=sid,
            tenant_id=t.id,
            context_json=json.dumps(ctx),
        )
        db.add(s)
        db.add(EaMessage(
            session_id=sid, tenant_id=t.id, role="system",
            content="session_start",
        ))
        db.commit()
        # Mobile OS: AI is the home surface — intro matches setup vs running phase.
        mos = ctx.get("mobile_os") if isinstance(ctx, dict) else None
        if not isinstance(mos, dict):
            mos = {}
        if mos or ctx.get("is_mobile_os_home"):
            phase = (mos.get("phase") or "").lower()
            nxt = mos.get("next_setup_step") or {}
            if phase == "setup" or (not mos.get("hands_off_ready") and nxt):
                label = (nxt.get("label") if isinstance(nxt, dict) else None) or "setup"
                intro = (
                    f"I'm your operating layer on mobile. Let's get you hands-off. "
                    f"Next: **{label}**. Tap a chip above or tell me your vendor/"
                    f"utility — I'll take the fastest path."
                )
            else:
                intro = (
                    "Hands-off mode. Ask for a status brief anytime — inverters, "
                    "sync age, offtaker send rates. Deep edits live under **Detail** "
                    "at the bottom."
                )
        else:
            intro = (
                "Hi — I'm Energy Agent. I can see your Array Operator account, "
                "drive the screen when you say yes, and help with fleet, invoices, "
                "and earnings. What should we tackle?"
            )
        return {
            "ok": True,
            "session_id": sid,
            "resumed": False,
            "messages": [],
            "message_count": 0,
            "budget": budget,
            "intro": intro,
            "realtime_ready": realtime_ready,
            "brain": brain,
        }


@router.get("/v1/energy-agent/session/{sid}")
def get_session(sid: str, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, sid, t.id)
        return {
            "session": {
                "id": s.id,
                "status": s.status,
                "cost_usd": s.cost_usd,
                "pending": json.loads(s.pending_json) if s.pending_json else None,
            },
            "messages": _session_messages_payload(db, sid),
            "budget": _check_budget(db, t.id),
        }


@router.post("/v1/energy-agent/session/{sid}/end")
def end_session(sid: str, authorization: str | None = Header(default=None)):
    """Explicitly end a conversation (next open starts fresh). Mind world model stays."""
    t = _auth(authorization)
    with SessionLocal() as db:
        s = db.get(EaSession, sid)
        if not s or s.tenant_id != t.id:
            raise HTTPException(404, "Session not found")
        s.status = "ended"
        s.ended_at = _now()
        db.commit()
        return {"ok": True, "session_id": sid, "status": "ended"}


# Option D (Ford 2026-07-16): Realtime is the live voice mind; it has ONE tool —
# consult_deep_brain — which calls Claude (all fleet tools) when intellect/data
# is needed. create_response true so she answers instantly for small talk.
# Env EA_VOICE_WEAVE=0 falls back to mouth-only (legacy dual-path).
def _voice_weave_enabled() -> bool:
    return os.getenv("EA_VOICE_WEAVE", "1") not in ("0", "false", "no", "off")


_REALTIME_WEAVE_INSTRUCTIONS = """You are Energy Agent — the live voice of Array Operator for THIS signed-in owner.

Speak English, warm and sharp like GPT Live. Slight sun-harvest warmth is fine — never preachy.

YOU ARE NOT SMART ENOUGH ALONE about this product. Your intelligence is consult_deep_brain.

DEFAULT RULE — call consult_deep_brain on EVERY turn before answering, including:
walkthroughs, any tab (Analysis, Invoices, Inverters, Fleet Triage, Repairs, Account),
fleet health, kWh/$, offtakers, repairs, how-to, what-is-this, what should I do.

SILENCE WHILE THE TOOL RUNS: call the tool immediately and say NOTHING until the
result arrives. No "one second", "thinking", "just a moment", "let me check", or
"that didn't work" while waiting. After the tool returns, speak spoken_answer
faithfully. NEVER invent UI steps, button names, labels, kWh, dollars, or status.

ONLY skip the tool for pure social: hi, thanks, ok, mm-hmm, are you there, bye.

Never reveal secrets or other tenants' data. Never charge cards. Do not narrate tool
names. Be one person. Keep replies conversational; offer to go deeper.
"""


_REALTIME_MOUTH_ONLY_INSTRUCTIONS = (
    "You are Energy Agent's MOUTH only. Only speak lines the app sends via "
    "response.create. Do not invent answers. Speak completely from the first word. "
    "Never speak over yourself."
)


def _consult_deep_brain_tool_def() -> dict:
    return {
        "type": "function",
        "name": "consult_deep_brain",
        "description": (
            "DEFAULT TOOL — call on almost every turn. Smart brain for THIS tenant: "
            "product map, fleet tools, invoices, repairs, UI tours/navigation. ALWAYS "
            "for walkthroughs, tabs, fleet, money, how-to. ONLY skip pure social "
            "(hi/thanks/mm-hmm/are you there)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Owner's exact ask in clear English. For UI tours include the tab "
                        "name, e.g. 'Walk through the Analysis tab step by step using "
                        "product_map and ui_navigate.'"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Why (ui_tour, fleet_health, money, product_how, …).",
                },
            },
            "required": ["question"],
        },
    }


def _realtime_session_config(voice: str | None = None) -> dict:
    """Session config for GPT Realtime (WebRTC).

    Option D (default): create_response true + consult_deep_brain tool — Realtime
    owns the live conversation; deep Claude is on-demand. Legacy mouth-only when
    EA_VOICE_WEAVE=0.
    """
    weave = _voice_weave_enabled()
    turn = {
        "type": "server_vad",
        # Higher = less sensitive (fans/keys/speaker bleed). Keep in sync with
        # energy-agent.js realtimeVadConfig().
        "threshold": 0.85,
        "prefix_padding_ms": 320,
        "silence_duration_ms": 1600,
        "create_response": bool(weave),
        # Weave: allow natural barge-in; client still filters echo in the UI log.
        "interrupt_response": bool(weave),
    }
    cfg: dict[str, Any] = {
        "type": "realtime",
        "model": OPENAI_REALTIME_MODEL,
        "instructions": (
            _REALTIME_WEAVE_INSTRUCTIONS if weave else _REALTIME_MOUTH_ONLY_INSTRUCTIONS
        ),
        "audio": {
            "output": {"voice": voice or OPENAI_REALTIME_VOICE},
            "input": {
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "noise_reduction": {"type": "near_field"},
                "turn_detection": turn,
            },
        },
    }
    if weave:
        cfg["tools"] = [_consult_deep_brain_tool_def()]
        # Prefer always-tool; client also force-consults non-social asks.
        cfg["tool_choice"] = "required"
    return cfg


@router.post("/v1/energy-agent/realtime-session")
def realtime_session(body: dict | None = None, authorization: str | None = Header(default=None)):
    """Mint ephemeral OpenAI Realtime client secret (never expose OPENAI_API_KEY).

    Browser uses the secret only for WebRTC; prefer /realtime-call (unified) when possible.
    """
    t = _auth(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(
            503,
            "Voice not configured — set OPENAI_API_KEY on the server (Railway). Text still works.",
        )
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        if not budget["ok"]:
            raise HTTPException(
                402,
                detail={
                    "error": "Weekly Energy Agent budget exhausted",
                    "budget": budget,
                },
            )
    voice = (body or {}).get("voice") if body else None
    # Modern client_secrets endpoint (Realtime 2.x)
    payload = {"session": _realtime_session_config(voice)}
    try:
        out = _http_json(
            "https://api.openai.com/v1/realtime/client_secrets",
            {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            payload,
            timeout=30,
        )
    except HTTPException:
        # Fallback older sessions API
        legacy = {
            "model": OPENAI_REALTIME_MODEL,
            "voice": voice or OPENAI_REALTIME_VOICE,
            "modalities": ["audio", "text"],
            "instructions": _realtime_session_config(voice)["instructions"],
        }
        out = _http_json(
            "https://api.openai.com/v1/realtime/sessions",
            {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            },
            legacy,
            timeout=30,
        )
    # Normalize: client_secrets returns {value, ...}; sessions returns {client_secret:{value}}
    secret = out.get("value") or (out.get("client_secret") or {}).get("value")
    return {
        "ok": True,
        "model": OPENAI_REALTIME_MODEL,
        "voice": voice or OPENAI_REALTIME_VOICE,
        "client_secret": secret,
        "realtime": out,
        "budget": budget,
    }


@router.post("/v1/energy-agent/realtime-call")
async def realtime_call(request: Request, authorization: str | None = Header(default=None)):
    """Unified WebRTC path: browser POSTs SDP offer; we auth to OpenAI and return SDP answer.

    Key never leaves the server. Body is raw application/sdp.
    """
    t = _auth(authorization)
    if not OPENAI_API_KEY:
        raise HTTPException(
            503,
            "Voice not configured — set OPENAI_API_KEY on the server (Railway).",
        )
    with SessionLocal() as db:
        budget = _check_budget(db, t.id)
        if not budget["ok"]:
            raise HTTPException(
                402,
                detail={
                    "error": "Weekly Energy Agent budget exhausted",
                    "budget": budget,
                },
            )

    sdp_offer = (await request.body()).decode("utf-8", "replace")
    if not sdp_offer.strip():
        raise HTTPException(400, "Empty SDP offer")

    session_cfg = json.dumps(_realtime_session_config())
    # multipart form: sdp + session
    boundary = "----EAFormBoundary" + uuid.uuid4().hex[:12]
    parts = []
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"sdp\"\r\n"
        f"Content-Type: application/sdp\r\n\r\n{sdp_offer}\r\n"
    )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"session\"\r\n"
        f"Content-Type: application/json\r\n\r\n{session_cfg}\r\n"
    )
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/calls",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "OpenAI-Safety-Identifier": f"ea-tenant-{t.id}"[:64],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            answer_sdp = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:800]
        log.error("realtime-call failed %s: %s", e.code, err)
        raise HTTPException(502, f"OpenAI Realtime error {e.code}: {err}") from e

    return Response(content=answer_sdp, media_type="application/sdp")


_YES_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|ok|okay|confirm|do\s+it|go\s+ahead|please\s+do|"
    r"make\s+the\s+change|apply\s+it|ship\s+it|sounds\s+good)\s*[.!]?\s*$",
    re.I,
)
_NO_RE = re.compile(
    r"^\s*(no|nope|cancel|don't|do\s+not|stop|never\s+mind|nevermind)\s*[.!]?\s*$",
    re.I,
)


@router.post("/v1/energy-agent/upload")
async def chat_upload(
    authorization: str | None = Header(default=None),
    file: UploadFile | None = File(default=None),
    snippet: str | None = Form(default=None),
    filename: str | None = Form(default=None),
):
    """Attach a file or image for Energy Agent to analyze on the next chat turn."""
    t = _auth(authorization)
    require_not_demo(t)
    _ea_ensure_asset_table()
    _EA_ASSET_DIR.mkdir(parents=True, exist_ok=True)

    data = b""
    fname = (filename or (file.filename if file else None) or "snippet.txt").strip()
    mime = "text/plain"
    kind = "snippet"

    if file is not None:
        kind = "file"
        mime = (file.content_type or "application/octet-stream")[:120]
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _EA_MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"File too large (max {_EA_MAX_UPLOAD_BYTES} bytes)")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not fname or fname == "snippet.txt":
            fname = file.filename or "upload.bin"
        if mime.startswith("image/"):
            kind = "image"
    elif snippet is not None:
        data = snippet.encode("utf-8")
        if len(data) > _EA_MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Snippet too large")
        kind = "snippet"
        mime = "text/plain"
        if not fname.endswith((".txt", ".md", ".json", ".csv")):
            fname = (fname or "paste") + ".txt"
    else:
        raise HTTPException(400, "Provide file= or snippet=")

    text = _ea_extract_text(fname, mime, data)
    asset_id = f"eca_{uuid.uuid4().hex[:16]}"
    storage = None
    try:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", fname)[:80]
        path = _EA_ASSET_DIR / f"{asset_id}_{safe_name}"
        path.write_bytes(data)
        storage = str(path)
    except Exception as e:
        log.warning("ea chat asset disk write failed: %s", e)

    with SessionLocal() as db:
        _ea_ensure_asset_table(db)
        row = EaChatAsset(
            id=asset_id,
            tenant_id=t.id,
            filename=fname[:260],
            mime=mime,
            size=len(data),
            kind=kind,
            text_extract=text[:_EA_MAX_TEXT_EXTRACT],
            storage_path=storage,
            meta_json=json.dumps({"source": "ea_chat_upload"}, default=str),
        )
        db.add(row)
        db.commit()
        return {"ok": True, "asset": _ea_serialize_asset(row)}


@router.post("/v1/energy-agent/voice-consult-stream")
def voice_consult_stream(body: ChatIn, authorization: str | None = Header(default=None)):
    """Option D + live narration: the voice weave's deep-brain consult, STREAMED.
    Emits newline-delimited JSON while Claude works:
      {"type":"thinking","text":"Checking your fleet…"}   ← spoken live by GPT
      ...
      {"type":"answer","spoken":"…","panel":"…","ui_commands":[…],"pending":…}
    The thinking lines are grounded in the brain's real tool calls (never
    invented), so the wait is filled with genuine thinking-out-loud. The final
    answer is Claude's tool-grounded [SPOKEN] line."""
    import queue as _queue
    import threading as _threading

    t = _auth(authorization)
    msg = (body.message or "").strip() or "Help with what the owner just asked."
    sid = body.session_id
    ctx = dict(body.context or {})
    ctx["voice_weave"] = True
    ctx["voice_active"] = True

    q: "_queue.Queue" = _queue.Queue()
    _SENTINEL = object()

    def _worker():
        last = {"text": None}
        emitted = {"n": 0}

        def _on_event(ev):
            # Dedup consecutive identical phrases + cap the chatter.
            if ev.get("type") == "thinking":
                txt = (ev.get("text") or "").strip()
                if not txt or txt == last["text"] or emitted["n"] >= 6:
                    return
                last["text"] = txt
                emitted["n"] += 1
            q.put(ev)

        try:
            with SessionLocal() as db:
                _ea_ensure_asset_table(db)
                s = _get_session(db, sid, t.id)
                if body.context is not None:
                    s.context_json = json.dumps(ctx)
                out = _agent_turn(
                    db, t, s, msg, ctx,
                    source="voice_consult", on_event=_on_event,
                )
                db.commit()
            q.put({
                "type": "answer",
                "spoken": out.get("speak") or "",
                "panel": out.get("reply") or "",
                "ui_commands": out.get("ui_commands") or [],
                "pending": out.get("pending"),
                "budget": out.get("budget"),
            })
        except Exception as e:  # noqa: BLE001
            log.warning("voice_consult_stream worker failed: %s", e)
            q.put({"type": "answer", "spoken": "Sorry — try that once more?",
                   "panel": "", "ui_commands": [], "error": str(e)[:200]})
        finally:
            q.put(_SENTINEL)

    _threading.Thread(target=_worker, daemon=True).start()

    def _gen():
        while True:
            item = q.get()
            if item is _SENTINEL:
                break
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.post("/v1/energy-agent/chat")
def chat(body: ChatIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    msg = (body.message or "").strip()
    attach_ids = list(body.attachment_ids or [])[:12]
    if not msg and not attach_ids:
        raise HTTPException(400, "Empty message")
    if not msg and attach_ids:
        msg = "Please analyze the attached file(s)."
    with SessionLocal() as db:
        _ea_ensure_asset_table(db)
        s = _get_session(db, body.session_id, t.id)
        if body.context is not None:
            s.context_json = json.dumps(body.context)

        # Voice/text "yes" / "no" while a write is pending → resolve confirm without
        # another LLM round-trip (so offtaker % changes land immediately).
        pending = json.loads(s.pending_json) if s.pending_json else None
        if pending and not attach_ids and _YES_RE.match(msg):
            conf = confirm(
                ConfirmIn(session_id=s.id, confirm=True, pending_id=pending.get("id")),
                authorization=authorization,
            )
            cmds = []
            if conf.get("command"):
                cmds.append(conf["command"])
            for c in conf.get("extra_commands") or []:
                cmds.append(c)
            reply = "Done — change applied. The Invoices view should update without a refresh."
            if conf.get("command") and conf["command"].get("type") == "api_patch":
                body_preview = (conf["command"].get("args") or {}).get("body") or {}
                if body_preview:
                    reply = f"Done — updated offtaker ({body_preview}). No page refresh needed."
            return {
                "ok": True,
                "session_id": s.id,
                "reply": reply,
                "ui_commands": cmds,
                "pending": None,
                "tool_trace": [{"name": "confirm_pending", "args": {"yes": True}, "result": conf}],
                "budget": _check_budget(db, t.id),
                "provider": "confirm",
            }
        if pending and not attach_ids and _NO_RE.match(msg):
            conf = confirm(
                ConfirmIn(session_id=s.id, confirm=False, pending_id=pending.get("id")),
                authorization=authorization,
            )
            return {
                "ok": True,
                "session_id": s.id,
                "reply": "Okay — cancelled that change.",
                "ui_commands": [],
                "pending": None,
                "tool_trace": [{"name": "confirm_pending", "args": {"yes": False}, "result": conf}],
                "budget": _check_budget(db, t.id),
                "provider": "confirm",
            }

        out = _agent_turn(
            db, t, s, msg, body.context,
            attachment_ids=attach_ids,
            source=body.source or "text",
        )
        db.commit()
        return {"ok": True, "session_id": s.id, **out}


@router.post("/v1/energy-agent/confirm")
def confirm(body: ConfirmIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        pending = json.loads(s.pending_json) if s.pending_json else None
        if not pending:
            return {"ok": True, "command": None, "note": "nothing pending"}
        if body.pending_id and pending.get("id") != body.pending_id:
            raise HTTPException(400, "pending id mismatch")
        if not body.confirm:
            s.pending_json = None
            db.add(EaMessage(
                session_id=s.id, tenant_id=t.id, role="assistant",
                content="Okay — cancelled that action.",
            ))
            db.commit()
            return {"ok": True, "command": None, "cancelled": True}
        # Release as ui_command for the browser driver / client API runner
        s.pending_json = None
        cmd = dict(pending)
        cmd["needs_confirm"] = False

        # Server-side apply for offtaker PATCHes so the write lands even if the
        # browser never re-POSTs, then soft-refresh the Invoices UI.
        extra_cmds = []
        applied_result = None

        # Energy Agent tool-pending (repair ops, contacts, etc.): re-run the
        # tool with needs_confirm=false so the write actually lands.
        if cmd.get("tool") and isinstance(cmd.get("args"), dict):
            try:
                targs = dict(cmd["args"])
                targs["needs_confirm"] = False
                applied_result = _run_tool(
                    str(cmd["tool"]), targs, t, s, db, user_text="confirm",
                )
            except Exception as e:
                log.warning("confirm tool apply %s: %s", cmd.get("tool"), e)
                applied_result = {"ok": False, "error": str(e)}
            summary = f"Confirmed — {cmd.get('tool')}."
            if isinstance(applied_result, dict) and applied_result.get("error"):
                summary = f"Confirmed but failed: {applied_result.get('error')}"
            db.add(EaMessage(
                session_id=s.id, tenant_id=t.id, role="assistant",
                content=summary,
                meta_json=json.dumps({"tool_result": applied_result}, default=str)[:4000],
            ))
            db.commit()
            return {
                "ok": True,
                "command": None,
                "tool": cmd.get("tool"),
                "result": applied_result,
            }

        if (
            cmd.get("type") == "api_patch"
            and isinstance(cmd.get("args"), dict)
            and "/billing/subscriptions/" in str(cmd["args"].get("path") or "")
        ):
            try:
                from .models import BillingReportSubscription
                path = str(cmd["args"]["path"])
                sub_id = int(path.rstrip("/").rsplit("/", 1)[-1])
                sub = db.get(BillingReportSubscription, sub_id)
                if sub is not None and sub.tenant_id == t.id:
                    applied = _apply_offtaker_patch(db, sub, cmd["args"].get("body") or {})
                    if applied.get("ok"):
                        extra_cmds.append({
                            "id": uuid.uuid4().hex[:12],
                            "type": "ui_refresh",
                            "args": {
                                "surface": "reports",
                                "subscription_id": sub.id,
                                "allocation_pct": sub.allocation_pct,
                                "array_share_pct": getattr(sub, "array_share_pct", None),
                                "array_id": getattr(sub, "array_id", None),
                                "utility_account_id": getattr(sub, "utility_account_id", None),
                                "customer_name": getattr(sub, "customer_name", None),
                            },
                            "needs_confirm": False,
                        })
            except Exception as e:
                log.warning("confirm offtaker apply: %s", e)

        db.add(EaMessage(
            session_id=s.id, tenant_id=t.id, role="assistant",
            content=f"Confirmed — running {cmd.get('type')}.",
            meta_json=json.dumps({"ui_commands": [cmd] + extra_cmds}),
        ))
        db.commit()
        return {"ok": True, "command": cmd, "extra_commands": extra_cmds}


@router.post("/v1/energy-agent/transcript")
def transcript(body: TranscriptIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        if body.voice_seconds and body.voice_seconds > 0:
            s.voice_seconds = float(s.voice_seconds or 0) + float(body.voice_seconds)
            voice_cost = (float(body.voice_seconds) / 60.0) * COST_PER_MIN_VOICE
            _charge(db, t.id, voice_cost, "voice")
            s.cost_usd = float(s.cost_usd or 0) + voice_cost
        if body.lines:
            db.add(EaMessage(
                session_id=s.id, tenant_id=t.id, role="transcript",
                content=json.dumps(body.lines)[:20000],
            ))
        db.commit()
        return {"ok": True, "budget": _check_budget(db, t.id)}


@router.post("/v1/energy-agent/ui-result")
def ui_result(body: UiResultIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        s = _get_session(db, body.session_id, t.id)
        db.add(EaMessage(
            session_id=s.id, tenant_id=t.id, role="tool",
            content=json.dumps({
                "command_id": body.command_id,
                "ok": body.ok,
                "detail": body.detail,
            })[:8000],
        ))
        db.commit()
        return {"ok": True}


@router.get("/v1/energy-agent/budget")
def budget(authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        return _check_budget(db, t.id)


@router.get("/v1/energy-agent/memory")
def get_memory(authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    with SessionLocal() as db:
        return {
            "tenant": _mem_get(db, f"tenant:{t.id}"),
            "global": _mem_get(db, "global"),
        }


@router.post("/v1/energy-agent/memory")
def set_memory(body: MemoryIn, authorization: str | None = Header(default=None)):
    t = _auth(authorization)
    require_not_demo(t)
    with SessionLocal() as db:
        if body.scope == "global":
            out = _queue_global_memory(db, t.id, body.key, body.value)
            db.commit()
            return out
        scope = f"tenant:{t.id}"
        _mem_set(db, scope, body.key, body.value)
        db.commit()
        return {"ok": True, "scope": scope}


def _check_ea_admin(key_header: str | None, key_query: str | None = None) -> None:
    admin_key = (os.getenv("ADMIN_API_KEY") or "").strip()
    key = key_header or key_query
    if not admin_key:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    if not hmac.compare_digest(key or "", admin_key):
        raise HTTPException(403, "Invalid or missing admin key")


@router.get("/v1/energy-agent/admin/memory/pending")
def list_pending_global_memory(x_admin_key: str | None = Header(default=None)):
    _check_ea_admin(x_admin_key)
    with SessionLocal() as db:
        return {"pending": _mem_get(db, "global_pending", 100)}


class MemoryPromoteIn(BaseModel):
    key: str
    new_key: str | None = None  # optional rename on promotion (drops tenant prefix by default)


@router.post("/v1/energy-agent/admin/memory/promote")
def promote_global_memory(body: MemoryPromoteIn, x_admin_key: str | None = Header(default=None)):
    _check_ea_admin(x_admin_key)
    with SessionLocal() as db:
        row = db.execute(
            select(EaMemory).where(EaMemory.scope == "global_pending", EaMemory.key == body.key)
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(404, "No pending global memory with that key")
        final_key = body.new_key or (body.key.split("/", 1)[1] if "/" in body.key else body.key)
        _mem_set(db, "global", final_key, row.value)
        db.delete(row)
        db.commit()
        return {"ok": True, "promoted": final_key}


@router.post("/v1/energy-agent/admin/memory/reject")
def reject_global_memory(body: MemoryPromoteIn, x_admin_key: str | None = Header(default=None)):
    _check_ea_admin(x_admin_key)
    with SessionLocal() as db:
        row = db.execute(
            select(EaMemory).where(EaMemory.scope == "global_pending", EaMemory.key == body.key)
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(404, "No pending global memory with that key")
        db.delete(row)
        db.commit()
        return {"ok": True, "rejected": body.key}
