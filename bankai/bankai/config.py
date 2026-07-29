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

SIMPLEFIN_ACCESS_URL = _env("SIMPLEFIN_ACCESS_URL")

NOTIFY_EMAILS = [e.strip() for e in _env("NOTIFY_EMAILS").split(",") if e.strip()]
NOTIFY_FROM = _env("NOTIFY_FROM", "bankai@localhost")
RESEND_API_KEY = _env("RESEND_API_KEY")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")

SYNC_INTERVAL_MINUTES = int(_env("SYNC_INTERVAL_MINUTES", "360") or 360)
RULES_INTERVAL_MINUTES = int(_env("RULES_INTERVAL_MINUTES", "15") or 15)
PORT = int(_env("PORT", "8300") or 8300)
