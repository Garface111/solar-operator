"""Clear user direction must skip Energy Agent write confirmation."""
from api.energy_agent import _user_clearly_directed


def test_share_percent_clearly_directed():
    assert _user_clearly_directed(
        "The share percent. I'd like to change it to 15%.",
        {"share_pct": 15},
    )
    assert _user_clearly_directed("change share to 20%", {"allocation_pct": 20})
    assert _user_clearly_directed("set it to 12.5 percent", {})


def test_yes_shortcut():
    assert _user_clearly_directed("yes")
    assert _user_clearly_directed("go ahead")
    assert _user_clearly_directed("do it")


def test_vague_edit_not_directed():
    # No value / field — should still clarify, not auto-apply an empty write
    assert not _user_clearly_directed("can you help me edit the town of Glover?")
    assert not _user_clearly_directed("what can you change?")


def test_email_clearly_directed():
    assert _user_clearly_directed(
        "update their email to bob@example.com",
        {"email": "bob@example.com"},
    )
