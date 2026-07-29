"""The Resend receiving inbox is shared with other systems on the same domain.

A household finance copilot also receives at agent.arrayoperator.com. Before this
guard, its owner's private email was pulled in, matched to a repair ticket, and
answered as the Energy Agent. Nothing outside our own mailboxes may be touched.
"""
from api.repair_ops import _is_our_mailbox, ingest_inbound_email


def test_our_mailboxes_are_accepted():
    assert _is_our_mailbox(['repairs@agent.arrayoperator.com'])
    assert _is_our_mailbox(['Energy Agent <agent@agent.arrayoperator.com>'])
    assert _is_our_mailbox(['sovereign@agent.arrayoperator.com'])


def test_another_systems_mailbox_is_rejected():
    # the household finance copilot shares this receiving domain
    assert not _is_our_mailbox(['copilot@agent.arrayoperator.com'])
    assert not _is_our_mailbox(['ford.genereaux@gmail.com'])
    assert not _is_our_mailbox([])
    assert not _is_our_mailbox(None)


def test_mail_to_a_foreign_mailbox_is_never_ingested():
    """No DB session is needed: the guard must reject before any lookup."""
    out = ingest_inbound_email(
        None,
        from_email='ford.genereaux@gmail.com',
        to_emails=['copilot@agent.arrayoperator.com'],
        subject='our mortgage statement',
        body='testing',
    )
    assert out['ok'] is False
    assert out['reason'] == 'not_our_mailbox'
