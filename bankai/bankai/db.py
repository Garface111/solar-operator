"""Database engine + session factory. SQLite by default, Postgres via DATABASE_URL."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from . import config

_is_sqlite = config.DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args, future=True)

if _is_sqlite:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL lets the claude-cli MCP server (a separate process) read and write
        # while the web app holds its own transactions; busy_timeout waits out
        # brief lock contention instead of failing immediately.
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    from . import models
    from . import goals  # noqa: F401 — registers the goals table on Base.metadata
    from . import watchpoints  # noqa: F401 — registers the watchpoints table
    from .connectors import resend_inbound  # noqa: F401 — registers inbound_emails
    from . import accounts_terms  # noqa: F401 — registers account_terms

    models.Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
