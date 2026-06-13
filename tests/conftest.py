"""
Shared pytest fixtures for the NEPOOL Operator API.

Critically, the DB URL + Stripe env must be set BEFORE any `api.*` module is
imported, because `api/db.py` and `api/onboarding.py` read them at import time.
We therefore mutate os.environ at the very top of this file (pytest imports
conftest.py before collecting test modules).
"""
from __future__ import annotations

import os
import tempfile

# ── isolate the test DB (a throwaway sqlite file) ───────────────────────────
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="solar-test-"), "test.db")
os.environ.pop("DATABASE_URL", None)          # don't accidentally hit prod PG
os.environ["SOLAR_DB_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)  # webhook uses unsigned construct_from
os.environ.pop("RESEND_API_KEY", None)         # never send real email in tests

import pytest
from fastapi.testclient import TestClient

from api.db import init_db, engine
from api.models import Base


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    init_db()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client():
    """A TestClient that does NOT trigger the app's startup event (so the
    APScheduler doesn't spin up during tests). Schema is created by the
    session fixture above."""
    from api.app import app
    return TestClient(app)
