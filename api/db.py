"""
DB engine + session helpers. SQLite for dev, Postgres on Railway.
Reads DATABASE_URL (Railway convention) first, then SOLAR_DB_URL.
"""
import os, pathlib
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from .models import Base

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
# for Postgres. Defaults raise the ceiling from SQLAlchemy's stock 5+10=15 to
# 15+15=30 concurrent connections per web process — enough to absorb a burst of
# sign-ups through the sync-route threadpool without exhausting the pool. Both
# knobs are env-tunable; keep total (size+overflow) * WEB_CONCURRENCY under the
# Postgres max_connections limit. pool_recycle avoids stale-conn errors after
# Postgres idle timeouts.
_is_sqlite = DB_URL.startswith("sqlite")
_pool_kwargs = {} if _is_sqlite else {
    "pool_size": int(os.environ.get("DB_POOL_SIZE", "15")),
    "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "15")),
    "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "1800")),
    "pool_timeout": int(os.environ.get("DB_POOL_TIMEOUT", "30")),
}

# Postgres session backstops (2026-07-09 whole-API meltdowns). Root class: a
# job/thread holds a transaction open across a slow external call (vendor HTTP
# can hang 30s+), its row locks never release, and with lock_timeout=0 every
# request touching those rows waits FOREVER — each one permanently eating a
# request-threadpool worker + a pool connection until even /health hangs.
# These make the whole class self-healing instead of a full outage:
#   • idle_in_transaction_session_timeout — a zombie "idle in transaction"
#     session is killed by Postgres itself, releasing its locks (2 min: far
#     above any legit gap between statements, far below outage territory).
#   • lock_timeout — a blocked statement errors after 15s instead of queueing
#     forever; the request 500s (visible, retryable) instead of freezing the app.
#   • statement_timeout — no single runaway query can wedge a worker (3 min;
#     migrations/backfills run outside this engine's defaults or re-SET locally).
# All env-tunable; 0 disables. Applied via PG session GUCs at connect time.
_pg_options = " ".join(
    f"-c {k}={v}" for k, v in (
        ("idle_in_transaction_session_timeout",
         os.environ.get("DB_IDLE_TXN_TIMEOUT_MS", "120000")),
        ("lock_timeout", os.environ.get("DB_LOCK_TIMEOUT_MS", "15000")),
        ("statement_timeout", os.environ.get("DB_STATEMENT_TIMEOUT_MS", "180000")),
    ) if str(v) != "0"
)
_connect_args = (
    {"check_same_thread": False} if _is_sqlite
    else ({"options": _pg_options} if _pg_options else {})
)
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
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        return db
    finally:
        pass  # caller closes
