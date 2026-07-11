"""Cloud Capture configuration — all env-driven, read at call time.

Safe by default: the global switch is OFF, so deploying the harvester image
without setting CLOUD_CAPTURE_ENABLED does nothing (no logins collected, no
browsers launched). Real-customer activation is a deliberate flip.
"""
from __future__ import annotations

import os


def _flag(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    """Global kill-switch. OFF by default. When False the scheduler runs no jobs
    and credential collection is refused — the whole subsystem is inert."""
    return _flag("CLOUD_CAPTURE_ENABLED", False)


def collection_enabled() -> bool:
    """Whether the API will ACCEPT a password upload into the server-side vault.
    Separate from `enabled()` so passwords can be collected (and encryption
    verified) before any harvesting is switched on. Still requires a crypto key."""
    return _flag("CLOUD_CAPTURE_COLLECT", False)


def allow_real_customers() -> bool:
    """Extra gate: run harvesting against tenants that are NOT demo/test.
    Defaults OFF so the farm only touches Ford's own/opted test tenants until
    the anti-lockout behavior is proven in the wild. Real portals can LOCK OUT
    an account on repeated automated logins — this gate is the seatbelt."""
    return _flag("CLOUD_CAPTURE_REAL_CUSTOMERS", False)


# Concurrency: how many portal logins run in parallel. Kept low on purpose —
# a stampede of logins from one datacenter IP is the fastest way to get
# flagged. Scale by adding egress IPs, not by cranking this.
def concurrency() -> int:
    return max(1, _int("CLOUD_CAPTURE_CONCURRENCY", 3))


def headless() -> bool:
    return _flag("CLOUD_CAPTURE_HEADLESS", True)


def tick_seconds() -> int:
    """How often the scheduler wakes to enumerate due work."""
    return max(60, _int("CLOUD_CAPTURE_TICK_SECONDS", 900))


def nav_timeout_ms() -> int:
    return _int("CLOUD_CAPTURE_NAV_TIMEOUT_MS", 45000)


def action_timeout_ms() -> int:
    return _int("CLOUD_CAPTURE_ACTION_TIMEOUT_MS", 20000)


def per_login_jitter_seconds() -> float:
    """Max random delay before a job starts, so many due logins don't fire in
    a synchronized burst. Applied as [0, this]."""
    return _float("CLOUD_CAPTURE_JITTER_SECONDS", 8.0)


def proxy_url() -> str | None:
    """Optional upstream proxy (e.g. a residential/egress proxy) for portal
    traffic. Datacenter IPs are the most bot-flagged; a residential egress is
    the single biggest reliability lever if portals start blocking."""
    return (os.environ.get("CLOUD_CAPTURE_PROXY") or "").strip() or None


def screenshot_dir() -> str:
    return (os.environ.get("CLOUD_CAPTURE_SHOT_DIR") or "/tmp/harvester_shots").strip()


# A realistic, current desktop Chrome UA. Overridable if it ages out.
def user_agent() -> str:
    return (os.environ.get("CLOUD_CAPTURE_UA") or
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36").strip()
