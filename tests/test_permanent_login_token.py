"""Permanent (far-future) login tokens are multi-use hand-off links."""
from datetime import datetime, timedelta

from api.account import _login_token_is_permanent


class _Row:
    def __init__(self, expires_at):
        self.expires_at = expires_at


def test_permanent_predicate_far_future():
    assert _login_token_is_permanent(_Row(datetime.utcnow() + timedelta(days=400)))
    assert _login_token_is_permanent(_Row(datetime.utcnow() + timedelta(days=365 * 50)))


def test_permanent_predicate_normal_magic_link():
    assert not _login_token_is_permanent(_Row(datetime.utcnow() + timedelta(minutes=15)))
    assert not _login_token_is_permanent(_Row(datetime.utcnow() + timedelta(days=30)))
    assert not _login_token_is_permanent(_Row(None))
