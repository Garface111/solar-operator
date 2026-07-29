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

SIMPLEFIN_ACCESS_URL = _env("SIMPLEFIN_ACCESS_URL")

# --- Real estate: comps + AVM via RentCast (free tier: 50 requests/month) ---
RENTCAST_API_KEY = _env("RENTCAST_API_KEY")
REALESTATE_REFRESH_DAYS = int(_env("REALESTATE_REFRESH_DAYS", "7") or 7)

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
