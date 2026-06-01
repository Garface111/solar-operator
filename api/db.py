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

engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
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
