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
# headless `claude -p`) | grok (xAI API credits) ---
LLM_BACKEND = _env("LLM_BACKEND", "anthropic")
XAI_API_KEY = _env("XAI_API_KEY")
GROK_MODEL = _env("GROK_MODEL", "grok-4")
CLAUDE_CLI_BIN = _env("CLAUDE_CLI_BIN", "claude")
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

# --- Real estate: comps + AVM via RentCast (free tier: 50 requests/month) ---
RENTCAST_API_KEY = _env("RENTCAST_API_KEY")
REALESTATE_REFRESH_DAYS = int(_env("REALESTATE_REFRESH_DAYS", "7") or 7)

# --- Email document harvesting (Gmail app password over IMAP; read-only usage) ---
GMAIL_ADDRESS = _env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD")
IMAP_HOST = _env("IMAP_HOST", "imap.gmail.com")
EMAIL_HARVEST_DAYS = int(_env("EMAIL_HARVEST_DAYS", "7") or 7)
# Three-way email thread: "Ford:ford@x.com,Sam:sam@x.com". ONLY these addresses
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
# "Ford:+18025551234,Sam:+18025555678" — names label speakers in the thread
HOUSEHOLD_PHONES = _env("HOUSEHOLD_PHONES")
# Exact public URL Twilio posts to, for signature validation behind proxies.
# If unset, it is reconstructed from the request (X-Forwarded-Proto aware).
SMS_PUBLIC_URL = _env("SMS_PUBLIC_URL")
NOTIFY_SMS = _env("NOTIFY_SMS", "true").lower() != "false"

SYNC_INTERVAL_MINUTES = int(_env("SYNC_INTERVAL_MINUTES", "360") or 360)
RULES_INTERVAL_MINUTES = int(_env("RULES_INTERVAL_MINUTES", "15") or 15)
PORT = int(_env("PORT", "8300") or 8300)
