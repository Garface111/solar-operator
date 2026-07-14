"""
DB engine + session helpers. SQLite for dev, Postgres on Railway.
Reads DATABASE_URL (Railway convention) first, then SOLAR_DB_URL.

Pool hardening (2026-07-14 outage + 2026-07-09 meltdown class)
-------------------------------------------------------------
Root failure mode: every request-thread waits on a DB connection while all
connections are held (long txn / external HTTP while session open / lock
pile-up). With pool_timeout=30s the threadpool fills → even /health hung.

Defenses:
  • Fail-fast pool_timeout (default 8s) so workers free quickly under saturation
  • PG session GUCs kill idle-in-transaction zombies + lock/statement runaways
  • pool_use_lifo reuses hot connections under burst
  • Checkout counters + rate-limited internal alert at high utilization
  • pool_status() for /health (no checkout required)
  • dispose_pool() recovery path for the watchdog
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
from typing import Any, Generator, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import TimeoutError as SATimeoutError

from .models import Base

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("SOLAR_DATA_DIR", pathlib.Path(__file__).parent.parent / "storage"))
DATA_DIR.mkdir(exist_ok=True, parents=True)

# Prefer Railway's DATABASE_URL, then SOLAR_DB_URL, then sqlite fallback.
DB_URL = (os.environ.get("DATABASE_URL") or os.environ.get("SOLAR_DB_URL") or "").strip()
if not DB_URL:
    DB_URL = f"sqlite:///{DATA_DIR / 'solar.db'}"
# Railway/Heroku give postgres:// but SQLAlchemy 2 needs postgresql://
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Connection pool. SQLite ignores pool sizing (single-writer), so only tune it
# for Postgres. Defaults: 15+15=30 concurrent connections per web process.
# Keep total (size+overflow) * WEB_CONCURRENCY under Postgres max_connections
# (and leave headroom for the cloud-capture-harvester service).
_is_sqlite = DB_URL.startswith("sqlite")
# Fail-fast under saturation: 8s (was 30s). Waiting longer just fills the
# request threadpool and cascades into a full outage (2026-07-14).
_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "15"))
_MAX_OVERFLOW = int(os.environ.get("DB_MAX_OVERFLOW", "15"))
_POOL_TIMEOUT = float(os.environ.get("DB_POOL_TIMEOUT", "8"))
_POOL_RECYCLE = int(os.environ.get("DB_POOL_RECYCLE", "1800"))

_pool_kwargs: dict[str, Any] = {} if _is_sqlite else {
    "pool_size": _POOL_SIZE,
    "max_overflow": _MAX_OVERFLOW,
    "pool_recycle": _POOL_RECYCLE,
    "pool_timeout": _POOL_TIMEOUT,
    # LIFO: prefer recently-used connections under burst (better cache locality,
    # fewer half-dead sockets when traffic is spiky).
    "pool_use_lifo": True,
}

# Postgres session backstops (2026-07-09 whole-API meltdowns). Root class: a
# job/thread holds a transaction open across a slow external call (vendor HTTP
# can hang 30s+), its row locks never release, and with lock_timeout=0 every
# request touching those rows waits FOREVER — each one permanently eating a
# request-threadpool worker + a pool connection until even /health hangs.
#   • idle_in_transaction_session_timeout — zombie idle-in-txn killed by PG
#   • lock_timeout — blocked statement errors after 15s instead of forever
#   • statement_timeout — no single runaway query wedges a worker
# All env-tunable; 0 disables. Applied via PG session GUCs at connect time.
_pg_options = " ".join(
    f"-c {k}={v}" for k, v in (
        ("idle_in_transaction_session_timeout",
         os.environ.get("DB_IDLE_TXN_TIMEOUT_MS", "120000")),
        ("lock_timeout", os.environ.get("DB_LOCK_TIMEOUT_MS", "15000")),
        ("statement_timeout", os.environ.get("DB_STATEMENT_TIMEOUT_MS", "180000")),
    ) if str(v) != "0"
)
_connect_args: dict[str, Any]
if _is_sqlite:
    _connect_args = {"check_same_thread": False}
else:
    _connect_args = {}
    if _pg_options:
        _connect_args["options"] = _pg_options
    # Don't hang forever establishing a new physical connection when PG is sick
    _connect_args["connect_timeout"] = int(os.environ.get("DB_CONNECT_TIMEOUT_S", "5"))

engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
    **_pool_kwargs,
)

# SQLite-specific: enable FK enforcement
if DB_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


# ── Pool checkout telemetry (no-checkout status for /health + watchdog) ──────
_pool_lock = threading.Lock()
_pool_checked_out = 0
_pool_checkout_waits = 0          # total times a checkout blocked > 1s
_pool_timeouts = 0               # SA TimeoutError count (process lifetime)
_pool_high_water = 0
_last_pressure_alert_at = 0.0
_PRESSURE_ALERT_COOLDOWN_S = int(os.environ.get("DB_POOL_ALERT_COOLDOWN_S", "300"))


def _pool_capacity() -> int:
    if _is_sqlite:
        return 1
    try:
        return int(engine.pool.size()) + int(engine.pool._max_overflow)  # noqa: SLF001
    except Exception:
        return _POOL_SIZE + _MAX_OVERFLOW


if not _is_sqlite:
    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, conn_record, conn_proxy):  # noqa: ANN001, ARG001
        global _pool_checked_out, _pool_high_water
        with _pool_lock:
            _pool_checked_out += 1
            if _pool_checked_out > _pool_high_water:
                _pool_high_water = _pool_checked_out
        # Soft signal only — never raise from a pool event
        try:
            cap = _pool_capacity()
            if cap > 0 and _pool_checked_out >= max(1, int(cap * 0.85)):
                _maybe_alert_pool_pressure(checked_out=_pool_checked_out, capacity=cap)
        except Exception:
            pass

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, conn_record):  # noqa: ANN001, ARG001
        global _pool_checked_out
        with _pool_lock:
            _pool_checked_out = max(0, _pool_checked_out - 1)


def _maybe_alert_pool_pressure(*, checked_out: int, capacity: int) -> None:
    """Rate-limited internal alert when the pool is near/at capacity."""
    global _last_pressure_alert_at
    now = time.monotonic()
    if now - _last_pressure_alert_at < _PRESSURE_ALERT_COOLDOWN_S:
        return
    _last_pressure_alert_at = now
    try:
        from .notify import send_internal_alert
        send_internal_alert(
            f"⚠ DB pool pressure: {checked_out}/{capacity} checked out",
            "SQLAlchemy connection pool is near exhaustion. If this persists, "
            "requests will 503 (fail-fast) instead of hanging the whole API.\n\n"
            f"checked_out={checked_out} capacity={capacity} "
            f"high_water={_pool_high_water} timeouts={_pool_timeouts}\n"
            "Likely causes: long txn held across external HTTP, runaway scheduler "
            "job, or lock pile-up. Watchdog will dispose the pool if it stays wedged.",
        )
    except Exception:
        logger.exception("pool pressure alert failed")


def record_pool_timeout() -> None:
    """Call from the SA TimeoutError handler so metrics stay honest."""
    global _pool_timeouts
    with _pool_lock:
        _pool_timeouts += 1


def pool_status() -> dict[str, Any]:
    """Snapshot of pool utilization. Never checks out a connection.

    Safe to call from async /health even when the sync threadpool is saturated.
    """
    if _is_sqlite:
        return {
            "dialect": "sqlite",
            "pool_size": 1,
            "max_overflow": 0,
            "capacity": 1,
            "checked_out": None,
            "checked_in": None,
            "overflow": None,
            "high_water": None,
            "timeouts": _pool_timeouts,
            "pool_timeout_s": None,
            "pressure": False,
        }
    pool = engine.pool
    try:
        size = int(pool.size())
    except Exception:
        size = _POOL_SIZE
    try:
        overflow = int(pool.overflow())
    except Exception:
        overflow = 0
    try:
        checked_in = int(pool.checkedin())
    except Exception:
        checked_in = None
    try:
        # SQLAlchemy QueuePool.checkedout() is authoritative when available
        checked_out = int(pool.checkedout())
    except Exception:
        with _pool_lock:
            checked_out = _pool_checked_out
    capacity = size + _MAX_OVERFLOW
    # pressure = most connections in use (warn /health + Sentry consumers)
    pressure = capacity > 0 and checked_out >= max(1, int(capacity * 0.85))
    with _pool_lock:
        hw = _pool_high_water
        timeouts = _pool_timeouts
    return {
        "dialect": "postgresql",
        "pool_size": size,
        "max_overflow": _MAX_OVERFLOW,
        "capacity": capacity,
        "checked_out": checked_out,
        "checked_in": checked_in,
        "overflow": overflow,
        "high_water": hw,
        "timeouts": timeouts,
        "pool_timeout_s": _POOL_TIMEOUT,
        "pressure": pressure,
    }


def dispose_pool(reason: str = "manual") -> dict[str, Any]:
    """Drop all pooled connections (in-use ones finish, then close).

    Recovery path for the watchdog when the pool is wedged with zombies that
    PG already killed but SQLAlchemy still thinks are checked out."""
    before = pool_status()
    try:
        engine.dispose()
        with _pool_lock:
            global _pool_checked_out
            _pool_checked_out = 0
        logger.warning("engine.pool disposed (%s) — was %s", reason, before)
    except Exception:
        logger.exception("engine.dispose failed (%s)", reason)
    after = pool_status()
    return {"before": before, "after": after, "reason": reason}


def ping_db(timeout_s: float = 2.0) -> bool:
    """Cheap SELECT 1. Returns False on any failure — never raises.

    Uses a short statement timeout locally so a wedged pool/PG can't hang the
    caller. Prefer not to call this from /health under pressure (skip when
    pool_status()['pressure'] is True)."""
    try:
        with engine.connect() as conn:
            if not _is_sqlite:
                try:
                    conn.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'"))
                except Exception:
                    pass
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("ping_db failed: %s", exc)
        return False


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI Depends-compatible session that ALWAYS closes.

    Prefer `with SessionLocal() as db:` in plain code — same cleanup contract.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass


# Re-export for exception handlers
PoolTimeout = SATimeoutError
