"""Regression: feature_suggestions.auto_prompt missing on existing tables.

Sentry: ProgrammingError UndefinedColumn feature_suggestions.auto_prompt
Culprit: /v1/sovereign/desk/ops → list_features SELECT includes mapped column
that create_all never added to an already-existing feature_suggestions table.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def legacy_fs_db(monkeypatch):
    """Session against a feature_suggestions table that predates auto_prompt."""
    monkeypatch.setenv("SOVEREIGN_ENABLED", "1")
    monkeypatch.setenv("SOVEREIGN_OPS_AUTHORITY", "1")

    import api.feature_suggestions as fs_mod
    import api.energy_agent_sovereign_ops as ops

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
        yield db, ops, fs_mod, engine

    fs_mod._reset_schema_ensure_for_tests()


def test_ensure_adds_auto_prompt_column(legacy_fs_db):
    db, ops, fs_mod, engine = legacy_fs_db
    cols_before = {
        r[1]
        for r in engine.connect().execute(text("PRAGMA table_info(feature_suggestions)"))
    }
    assert "auto_prompt" not in cols_before

    fs_mod.ensure_feature_suggestion_columns(db)

    cols_after = {
        r[1]
        for r in engine.connect().execute(text("PRAGMA table_info(feature_suggestions)"))
    }
    assert "auto_prompt" in cols_after


def test_list_features_survives_missing_auto_prompt_column(legacy_fs_db):
    """The Sentry path: list_features must not raise UndefinedColumn."""
    db, ops, fs_mod, engine = legacy_fs_db

    # Would raise ProgrammingError / OperationalError without ensure:
    # SELECT ... feature_suggestions.auto_prompt FROM feature_suggestions
    rows = ops.list_features(db, status="reviewed", limit=25)
    assert len(rows) == 1
    assert rows[0]["status"] == "reviewed"
    assert "dark mode" in (rows[0]["text"] or "")

    # Column is present after the self-heal path inside list_features
    cols = {
        r[1]
        for r in engine.connect().execute(text("PRAGMA table_info(feature_suggestions)"))
    }
    assert "auto_prompt" in cols


def test_ensure_is_idempotent(legacy_fs_db):
    db, ops, fs_mod, engine = legacy_fs_db
    fs_mod.ensure_feature_suggestion_columns(db)
    fs_mod._reset_schema_ensure_for_tests()
    fs_mod.ensure_feature_suggestion_columns(db)  # second apply after cache clear
    rows = ops.list_features(db, status="reviewed", limit=5)
    assert len(rows) == 1
