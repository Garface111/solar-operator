import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Tests must never touch a real database. The scheduler-loop tests go through
# bankai.db's module-level engine, which binds DATABASE_URL at import time — and
# a deployed tree carries a .env pointing at the LIVE household DB. A real
# environment variable beats the .env (load_dotenv never overrides), so pin it
# to a throwaway file before the first bankai import.
_TEST_DB = Path(tempfile.mkdtemp(prefix="bankai-tests-")) / "bankai-tests.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bankai.models import Base  # noqa: E402

# Tables defined outside models.py register on Base.metadata only when their
# module is imported, exactly as db.init_db does it. Importing them here keeps
# the test schema identical to the real one — otherwise a feature works in
# production and its tests fail on a missing table, or worse, the reverse.
from bankai import accounts_terms, goals, watchpoints  # noqa: E402,F401
from bankai.connectors import resend_inbound  # noqa: E402,F401

# The loop tests (tending, check-in) hit the module engine directly through
# session_scope, so the throwaway DB needs the real schema up front.
from bankai.db import init_db  # noqa: E402

init_db()


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
