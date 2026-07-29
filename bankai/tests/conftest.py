import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bankai.models import Base  # noqa: E402

# Tables defined outside models.py register on Base.metadata only when their
# module is imported, exactly as db.init_db does it. Importing them here keeps
# the test schema identical to the real one — otherwise a feature works in
# production and its tests fail on a missing table, or worse, the reverse.
from bankai import accounts_terms, goals, watchpoints  # noqa: E402,F401
from bankai.connectors import resend_inbound  # noqa: E402,F401


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
