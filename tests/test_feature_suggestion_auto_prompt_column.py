"""Regression: feature_suggestions.auto_prompt missing on existing tables.

Sentry: ProgrammingError UndefinedColumn feature_suggestions.auto_prompt —
a column mapped on the model that create_all never added to an already-existing
feature_suggestions table, so any SELECT naming it blew up in prod.

Originally driven through the Sovereign ops list_features endpoint; that caller
is gone, but the self-heal it exposed is product code and stays covered here.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def legacy_fs_db(monkeypatch):
    """Session against a feature_suggestions table that predates auto_prompt."""
    import api.feature_suggestions as fs_mod

    fs_mod._reset_schema_ensure_for_tests()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Pre-auto_prompt shape (no auto_prompt column)
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE feature_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME,
                product VARCHAR(32),
                email VARCHAR(255),
                tenant_id VARCHAR(40),
                text TEXT NOT NULL,
                status VARCHAR(16),
                review TEXT,
                reviewed_at DATETIME,
                screenshot_b64 TEXT
            )
            """
        ))
        conn.execute(text(
            "INSERT INTO feature_suggestions (text, status, product) "
            "VALUES ('Add dark mode to reports', 'reviewed', 'array_operator')"
        ))

    Session = sessionmaker(bind=engine)
    with Session() as db:
        yield db, fs_mod, engine

    fs_mod._reset_schema_ensure_for_tests()


def _cols(engine):
    return {
        r[1]
        for r in engine.connect().execute(text("PRAGMA table_info(feature_suggestions)"))
    }


def test_ensure_adds_auto_prompt_column(legacy_fs_db):
    db, fs_mod, engine = legacy_fs_db
    assert "auto_prompt" not in _cols(engine)

    fs_mod.ensure_feature_suggestion_columns(db)

    assert "auto_prompt" in _cols(engine)


def test_ensure_ddl_commits_outside_caller_transaction(legacy_fs_db):
    """ALTER must survive the caller's session.rollback() (PG recurrence class)."""
    db, fs_mod, engine = legacy_fs_db
    fs_mod.ensure_feature_suggestion_columns(db)
    # Caller aborts its ambient txn — DDL must already be committed separately.
    db.rollback()

    assert "auto_prompt" in _cols(engine)
    # A SELECT naming the column still works after the rollback.
    rows = db.execute(text(
        "SELECT auto_prompt FROM feature_suggestions WHERE status = 'reviewed'"
    )).fetchall()
    assert len(rows) == 1


def test_ensure_is_idempotent(legacy_fs_db):
    db, fs_mod, engine = legacy_fs_db
    fs_mod.ensure_feature_suggestion_columns(db)
    fs_mod._reset_schema_ensure_for_tests()
    fs_mod.ensure_feature_suggestion_columns(db)  # second apply after cache clear

    assert "auto_prompt" in _cols(engine)
    rows = db.execute(text("SELECT id FROM feature_suggestions")).fetchall()
    assert len(rows) == 1
