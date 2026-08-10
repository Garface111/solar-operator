"""Environment-driven configuration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


APP_PASSWORD = _env("APP_PASSWORD")
SESSION_SECRET = _env("SESSION_SECRET") or hashlib.sha256(
    ("bankai-session:" + APP_PASSWORD).encode()
).hexdigest()

DATABASE_URL = _env("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'bankai.db'}"

ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-opus-5")

# --- LLM backend: anthropic (API key) | claude-cli (Claude subscription via
# headless `claude -p`) | grok (xAI / Grok Build prepaid credits via OIDC or key) ---
# Comma-separated fallback chain is supported, e.g. "claude-cli,grok".
LLM_BACKEND = _env("LLM_BACKEND", "anthropic")
XAI_API_KEY = _env("XAI_API_KEY")  # classic console key; optional if Grok Build OIDC is live
GROK_MODEL = _env("GROK_MODEL", "grok-4")
# Prefer Grok Build OIDC (prepaid team) over a possibly-capped console API key.
# bankai.xai_auth reads ~/.grok/auth.json and falls back to Hermes ~/.hermes/auth.json.
XAI_PREFER_GROK_BUILD_OIDC = _env("XAI_PREFER_GROK_BUILD_OIDC", "1")
CLAUDE_CLI_BIN = _env("CLAUDE_CLI_BIN", "claude")
# Reasoning effort per turn (low|medium|high|xhigh|max). Empty = CLI default.
# Scoped to BankAI's own subprocesses — never set the CLI's global config for
# this; other agents on the machine share the same claude install.
CLAUDE_CLI_EFFORT = _env("CLAUDE_CLI_EFFORT")
# Per-turn wall clock for the headless CLI. Keep short (120) when claude-cli is
# a fallback behind another backend; give real headroom when it is the primary
# brain doing tool-heavy turns.
CLAUDE_CLI_TIMEOUT_SECONDS = int(_env("CLAUDE_CLI_TIMEOUT_SECONDS", "600") or 600)
CLAUDE_CLI_MODEL = _env("CLAUDE_CLI_MODEL")  # empty = the CLI's default model

# --- Reply verification: a second model pass critiques consequential replies
# (dollar figures, percentages, recommendations, deadlines) before they are
# sent, and revises them once if it finds a material problem. Costs one or two
# extra model calls on consequential turns only; set false to disable. ---
VERIFY_REPLIES = (_env("VERIFY_REPLIES", "true") or "true").lower() not in (
    "false",
    "0",
    "no",
    "off",
)

SIMPLEFIN_ACCESS_URL = _env("SIMPLEFIN_ACCESS_URL")
# Each spouse can hold their own SimpleFIN bridge, so neither has to hand their
# bank credentials to the other's account. Comma-separated; the single-URL form
# above still works and is treated as a list of one.
SIMPLEFIN_ACCESS_URLS = [
    u.strip() for u in (
        _env("SIMPLEFIN_ACCESS_URLS") or SIMPLEFIN_ACCESS_URL
    ).split(",") if u.strip()
]

# --- Real estate: comps + AVM via RentCast (free tier: 50 requests/month) ---
RENTCAST_API_KEY = _env("RENTCAST_API_KEY")
REALESTATE_REFRESH_DAYS = int(_env("REALESTATE_REFRESH_DAYS", "7") or 7)

# --- Email document harvesting (Gmail app password over IMAP; read-only usage) ---
GMAIL_ADDRESS = _env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD")
IMAP_HOST = _env("IMAP_HOST", "imap.gmail.com")
EMAIL_HARVEST_DAYS = int(_env("EMAIL_HARVEST_DAYS", "7") or 7)
# Three-way email thread: "Ford:ford@x.com,Partner:partner@example.com". ONLY these addresses
# are answered — the copilot can read the household's whole financial picture.
HOUSEHOLD_EMAILS = _env("HOUSEHOLD_EMAILS")
EMAIL_POLL_SECONDS = int(_env("EMAIL_POLL_SECONDS", "60") or 60)
# Outbound household mail goes out as this address. Sending prefers Resend
# (RESEND_API_KEY, shared with the notification path); the From domain must be
# verified in Resend. Falls back to GMAIL_ADDRESS over SMTP.
EMAIL_FROM = _env("EMAIL_FROM")
# Address the copilot RECEIVES at (defaults to EMAIL_FROM). Resend's inbound list
# also carries mail for the household's other agents, so this is the filter that
# keeps this copilot reading only its own conversations.
EMAIL_INBOUND_ADDRESS = _env("EMAIL_INBOUND_ADDRESS")

# --- The household's own Google Sheets planning model ---
# Reading works with link-sharing alone. Writing needs a Google service account
# (path to its JSON key) and the sheet shared with that account's address.
SHEETS_ID = _env("SHEETS_ID")
SHEETS_GID = _env("SHEETS_GID")  # empty = the workbook's default tab
# Name of the planning tab to READ. Preferred over relying on tab order: adding
# a tab changes which sheet the default export returns.
SHEETS_TAB = _env("SHEETS_TAB")
SHEETS_SERVICE_ACCOUNT_JSON = _env("SHEETS_SERVICE_ACCOUNT_JSON")
# Preferred write path: an Apps Script web app bound to the sheet. Needs no
# service-account key, so it works under Google's default org policy that blocks
# key creation. The secret is what gates the endpoint — treat it as a password.
SHEETS_WEBHOOK_URL = _env("SHEETS_WEBHOOK_URL")
SHEETS_WEBHOOK_SECRET = _env("SHEETS_WEBHOOK_SECRET")

NOTIFY_EMAILS = [e.strip() for e in _env("NOTIFY_EMAILS").split(",") if e.strip()]
NOTIFY_FROM = _env("NOTIFY_FROM", "bankai@localhost")
RESEND_API_KEY = _env("RESEND_API_KEY")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")

# --- SMS group thread (Twilio) ---
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")  # AC... (Console home)
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")  # required: signs inbound webhooks
# Optional standard API key (SK... + secret) — preferred auth for sending
TWILIO_API_KEY_SID = _env("TWILIO_API_KEY_SID")
TWILIO_API_KEY_SECRET = _env("TWILIO_API_KEY_SECRET")
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER")
# "Ford:+18025551234,Partner:+15555550002" — names label speakers in the thread
HOUSEHOLD_PHONES = _env("HOUSEHOLD_PHONES")
# Exact public URL Twilio posts to, for signature validation behind proxies.
# If unset, it is reconstructed from the request (X-Forwarded-Proto aware).
SMS_PUBLIC_URL = _env("SMS_PUBLIC_URL")
NOTIFY_SMS = _env("NOTIFY_SMS", "true").lower() != "false"

# --- WhatsApp group thread (Baileys bridge sidecar, whatsapp-bridge/) ---
# The bridge is a separate Node process holding the copilot's OWN WhatsApp
# session (never a person's — a ban would take their number with it). It spools
# group messages to WHATSAPP_DATA_DIR/inbound.jsonl; replies go out through
# WHATSAPP_DATA_DIR/outbox/.
WHATSAPP_ENABLED = _env("WHATSAPP_ENABLED", "false").lower() in ("true", "1", "yes", "on")
WHATSAPP_DATA_DIR = _env("WHATSAPP_DATA_DIR")  # e.g. /root/bankai-data/whatsapp
# Optional: answer only this group (JID like 1203...@g.us, from status.json's
# group list once the copilot has been added). Empty = any group it is in,
# still limited to household senders.
WHATSAPP_GROUP_JID = _env("WHATSAPP_GROUP_JID")
WHATSAPP_POLL_SECONDS = int(_env("WHATSAPP_POLL_SECONDS", "10") or 10)
# WhatsApp increasingly shows group senders as privacy LIDs (12309...@lid)
# instead of phone JIDs, so HOUSEHOLD_PHONES alone cannot always identify a
# spouse. "Ford:123098057695369,Gaurav:456..." — LIDs appear in the copilot's
# log the first time each person writes.
WHATSAPP_HOUSEHOLD_LIDS = _env("WHATSAPP_HOUSEHOLD_LIDS")

SYNC_INTERVAL_MINUTES = int(_env("SYNC_INTERVAL_MINUTES", "360") or 360)
# Wake the copilot for a self-directed look whenever a sync brings new
# transactions in from the banks. Silence is the expected outcome; it speaks
# (or emails, via email_household) only when the new data warrants it.
SYNC_WAKE = _env("SYNC_WAKE", "true").lower() != "false"
RULES_INTERVAL_MINUTES = int(_env("RULES_INTERVAL_MINUTES", "15") or 15)
# How often the copilot works on its own initiative with nobody watching.
TENDING_INTERVAL_HOURS = int(_env("TENDING_INTERVAL_HOURS", "6") or 6)
# --- The Saturday-morning printed report ---
# CUPS destination for the household printer (lpadmin-registered). On FordBrain:
# the Epson ET-2800 at 10.0.0.59, queue name "household".
PRINTER_NAME = _env("PRINTER_NAME", "household")
WEEKLY_REPORT = _env("WEEKLY_REPORT", "true").lower() != "false"
WEEKLY_REPORT_WEEKDAY = int(_env("WEEKLY_REPORT_WEEKDAY", "5") or 5)  # Mon=0 .. Sat=5
WEEKLY_REPORT_HOUR = int(_env("WEEKLY_REPORT_HOUR", "8") or 8)  # local time, on/after

# Every N days the copilot writes the household a short check-in — emailed to
# both spouses when the email channel is up, posted to the thread otherwise.
# Unlike tending, arriving is the point: it always speaks. 0 disables.
CHECKIN_INTERVAL_DAYS = int(_env("CHECKIN_INTERVAL_DAYS", "3") or 3)
PORT = int(_env("PORT", "8300") or 8300)

# --- Sentinel: the self-defense subsystem (security/sentinel.py) ---
# How often the posture self-audit + threat watch runs. It detects and alarms;
# it never changes security controls on its own.
SENTINEL_INTERVAL_MINUTES = int(_env("SENTINEL_INTERVAL_MINUTES", "60") or 60)
SENTINEL_ENABLED = _env("SENTINEL_ENABLED", "true").lower() != "false"
