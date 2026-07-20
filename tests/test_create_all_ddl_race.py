"""Regression: concurrent create_all must not 500 on Postgres type races.

Sentry: IntegrityError UniqueViolation pg_type_typname_nsp_index on
CREATE TABLE ea_email_delivery during api.db.init_db (web+worker boot race).
"""
from __future__ import annotations

import pytest
from sqlalchemy import MetaData
from sqlalchemy.exc import IntegrityError

from api.models import Base, _is_pg_concurrent_ddl_race


def test_pg_type_unique_violation_is_ddl_race():
    orig = Exception(
        'duplicate key value violates unique constraint "pg_type_typname_nsp_index"\n'
        "DETAIL:  Key (typname, typnamespace)=(ea_email_delivery, 2200) already exists."
    )
    exc = IntegrityError(
        "\nCREATE TABLE ea_email_delivery (...)",
        None,
        orig,
    )
    assert _is_pg_concurrent_ddl_race(exc) is True


def test_row_level_unique_violation_is_not_ddl_race():
    """Do not treat data uniques (e.g. uq_daily_array_day) as DDL races."""
    orig = Exception(
        'duplicate key value violates unique constraint "uq_daily_array_day"\n'
        "DETAIL:  Key (array_id, day)=(1, 2026-07-07) already exists."
    )
    exc = IntegrityError("INSERT INTO daily_generation ...", None, orig)
    assert _is_pg_concurrent_ddl_race(exc) is False


def test_create_all_retries_once_on_pg_type_race(monkeypatch):
    """Base.metadata.create_all retries once when peer wins the type race."""
    calls = {"n": 0}

    def flaky(self, bind=None, tables=None, checkfirst=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError(
                "\nCREATE TABLE ea_email_delivery (...)",
                None,
                Exception(
                    'duplicate key value violates unique constraint '
                    '"pg_type_typname_nsp_index"\n'
                    "DETAIL:  Key (typname, typnamespace)="
                    "(ea_email_delivery, 2200) already exists."
                ),
            )
        return None

    monkeypatch.setattr(MetaData, "create_all", flaky)
    # Must not raise — second super().create_all succeeds.
    Base.metadata.create_all(bind=None)
    assert calls["n"] == 2


def test_create_all_reraises_non_race_integrity_error(monkeypatch):
    def always_fail(self, bind=None, tables=None, checkfirst=True):
        raise IntegrityError(
            "something else",
            None,
            Exception("not a concurrent ddl race at all"),
        )

    monkeypatch.setattr(MetaData, "create_all", always_fail)
    with pytest.raises(IntegrityError, match="not a concurrent ddl race"):
        Base.metadata.create_all(bind=None)


def test_init_db_still_creates_tables():
    """Smoke: race-safe metadata still runs real create_all on the test DB."""
    # EaEmailDelivery lives on shared Base but is defined in energy_agent —
    # import so create_all sees it (app startup imports the router first).
    import api.energy_agent  # noqa: F401
    from api.db import init_db, engine
    from sqlalchemy import inspect

    init_db()
    names = set(inspect(engine).get_table_names())
    assert "tenants" in names
    assert "ea_email_delivery" in names
