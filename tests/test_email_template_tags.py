"""Guard the AI email-template generator against inventing merge tags.

Bruce reported (June 5): the Customize Report Email AI tried to enter a
variable that didn't exist in the system. {{whatever}} tags that aren't in
the allowlist render verbatim in the customer's inbox — broken-looking
text. The fix: server-side sanitization that strips any tag not in
ALLOWED_MERGE_TAGS, plus an operator-visible notice in the reply.
"""
from __future__ import annotations

from api.email_templates import (
    ALLOWED_MERGE_TAGS,
    _augment_reply_with_strip_notice,
    _strip_unknown_tags,
    extract_tags,
    unknown_tags,
)


def test_allowlist_contents_match_render_context():
    """The allowlist must include every tag the seed templates reference."""
    from api.email_templates import (
        DEFAULT_BODY_TEMPLATE,
        DEFAULT_SUBJECT_TEMPLATE,
        DEFAULT_SIGNOFF,
    )

    used = (
        extract_tags(DEFAULT_SUBJECT_TEMPLATE)
        | extract_tags(DEFAULT_BODY_TEMPLATE)
        | extract_tags(DEFAULT_SIGNOFF)
    )
    missing = used - ALLOWED_MERGE_TAGS
    assert not missing, (
        f"seed templates reference tags not in ALLOWED_MERGE_TAGS: {missing}"
    )


def test_extract_and_unknown_tags():
    body = (
        "<p>Hi {{client_name}}, your Q{{quarter}} bill total is "
        "{{quarter_total_mwh}} MWh.</p>"
    )
    assert extract_tags(body) == {"client_name", "quarter", "quarter_total_mwh"}
    assert unknown_tags(body) == {"quarter_total_mwh"}


def test_strip_unknown_tags_keeps_allowed_replaces_invented():
    body = (
        "<p>Dear {{client_name}}, {{period_start}} to {{period_end}}. "
        "Your {{quarter_total_mwh}} MWh and {{rec_count}} RECs.</p>"
    )
    out = _strip_unknown_tags(body)
    assert "{{client_name}}" in out
    assert "{{period_start}}" in out
    assert "{{period_end}}" in out
    assert "{{quarter_total_mwh}}" not in out
    assert "{{rec_count}}" not in out
    assert out.count("[…]") == 2


def test_strip_unknown_tags_passthrough_when_clean():
    body = "<p>Hi {{client_name}}, here's your {{quarter}} report.</p>"
    assert _strip_unknown_tags(body) == body


def test_strip_unknown_tags_handles_none_and_empty():
    assert _strip_unknown_tags(None) is None
    assert _strip_unknown_tags("") == ""


def test_strip_unknown_tags_handles_whitespace_inside_braces():
    """The tag regex allows inner whitespace — guard the strip path too."""
    body = "<p>{{ quarter_total_mwh }} MWh</p>"
    assert _strip_unknown_tags(body) == "<p>[…] MWh</p>"


def test_reply_notice_added_when_tags_stripped():
    reply = "I added a generation summary line."
    augmented = _augment_reply_with_strip_notice(
        reply,
        stripped_subject=[],
        stripped_body=["quarter_total_mwh", "rec_count"],
    )
    assert reply.rstrip() in augmented
    assert "{{quarter_total_mwh}}" in augmented
    assert "{{rec_count}}" in augmented
    assert "don't" in augmented or "don" in augmented


def test_reply_notice_omitted_when_clean():
    reply = "I tightened the tone."
    out = _augment_reply_with_strip_notice(
        reply, stripped_subject=[], stripped_body=[]
    )
    assert out == reply


def test_reply_notice_summarizes_when_many_bad_tags():
    out = _augment_reply_with_strip_notice(
        "fine.",
        stripped_subject=[],
        stripped_body=[f"made_up_{i}" for i in range(7)],
    )
    assert "+3 more" in out
