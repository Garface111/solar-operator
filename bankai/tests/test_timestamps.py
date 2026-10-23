"""Timestamp defaults: naive-UTC, and no longer routed through the deprecated
datetime.utcnow().

Every model default used to call datetime.utcnow() — deprecated in 3.12 and
slated for removal, so a Python upgrade would have started breaking timestamps
(and the suite printed the warning on every run). The fix swaps in models._utcnow,
which reads UTC the supported way and stores the same naive value as before. These
tests pin BOTH halves of that contract: the stored value is still naive UTC, and
the deprecated call is gone.
"""
import warnings
from datetime import datetime, timezone, timedelta

from bankai.models import Account, Initiative, _utcnow


def _reference_utc_now() -> datetime:
    """What 'now' looks like as naive UTC, computed the non-deprecated way — the
    yardstick the stored timestamps must land next to."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_utcnow_helper_returns_naive_utc():
    now = _utcnow()
    # Naive: existing rows and every naive datetime.utcnow() the rest of the app
    # still compares against are tz-less, so a tz-aware value here would silently
    # break "> cutoff" queries with a can't-compare-naive-and-aware TypeError.
    assert now.tzinfo is None
    # And it is genuinely UTC — not local time from a bare datetime.now(), which
    # would drift by the machine's offset. Off machines run in UTC, but the check
    # anchors the value to the real UTC wall clock regardless.
    assert abs(now - _reference_utc_now()) < timedelta(seconds=5)


def test_new_row_timestamp_default_is_naive_utc(session):
    acct = Account(name="Probe Checking")
    session.add(acct)
    session.flush()  # fires the column default, exactly as a real insert does

    assert acct.created_at is not None
    assert acct.created_at.tzinfo is None
    assert abs(acct.created_at - _reference_utc_now()) < timedelta(seconds=5)


def test_onupdate_timestamp_stays_naive_utc(session):
    init = Initiative(title="Probe initiative")
    session.add(init)
    session.flush()
    assert init.created_at.tzinfo is None
    assert init.updated_at.tzinfo is None

    # Touch a field and flush again so onupdate=_utcnow fires; it must stay naive.
    init.next_action = "do the next thing"
    session.flush()
    assert init.updated_at.tzinfo is None
    assert init.updated_at >= init.created_at


def test_default_no_longer_calls_deprecated_utcnow(session):
    """Would fail before the fix: on Python >=3.12 the old default=datetime.utcnow
    raised this very DeprecationWarning on every insert. We turn only the utcnow
    deprecation into an error (leaving unrelated warnings alone) and drive a real
    insert through it."""
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.filterwarnings("error", message=r".*utcnow.*")
        acct = Account(name="Warning Probe")
        session.add(acct)
        session.flush()  # no utcnow deprecation may escape this line
