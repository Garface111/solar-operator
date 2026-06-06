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
    DEFAULT_BODY_TEMPLATE,
    _augment_reply_with_strip_notice,
    _strip_unknown_tags,
    build_context,
    derive_client_first_name,
    extract_tags,
    render_email,
    unknown_tags,
)


def test_client_first_name_in_allowlist():
    assert "client_first_name" in ALLOWED_MERGE_TAGS


def test_derive_client_first_name_simple():
    assert derive_client_first_name("Bruce Genereaux") == "Bruce"


def test_derive_client_first_name_last_first_format():
    assert derive_client_first_name("Genereaux, Bruce") == "Bruce"


def test_derive_client_first_name_company():
    assert derive_client_first_name("Green Mountain Community Solar") == "Green"


def test_derive_client_first_name_trims_whitespace():
    assert derive_client_first_name("  Bruce  ") == "Bruce"


def test_derive_client_first_name_empty():
    assert derive_client_first_name("") == ""


def test_derive_client_first_name_single_word():
    assert derive_client_first_name("Madonna") == "Madonna"


def test_build_context_includes_first_name():
    ctx = build_context(
        client_name="Bruce Genereaux",
        tenant_name="Solar Co",
        arrays_count=2,
    )
    assert ctx["client_first_name"] == "Bruce"


def test_build_context_first_name_fallback_for_empty():
    """build_context falls back to full client_name when derive returns ''."""
    ctx = build_context(
        client_name="",
        tenant_name="Solar Co",
        arrays_count=1,
    )
    assert ctx["client_first_name"] == ""


def test_render_email_substitutes_client_first_name():
    ctx = build_context(
        client_name="Bruce Genereaux",
        tenant_name="Solar Co",
        arrays_count=2,
    )
    subject, html, _ = render_email(
        subject_template="Hi {{client_first_name}}",
        body_template="<p>Hello {{client_first_name}},</p>{{signoff}}",
        ctx=ctx,
    )
    assert subject == "Hi Bruce"
    assert "Hello Bruce," in html


def test_default_body_uses_client_first_name():
    assert "{{client_first_name}}" in DEFAULT_BODY_TEMPLATE
    assert "{{client_name}}" not in DEFAULT_BODY_TEMPLATE.split("{{signoff}}")[0].split("<p>")[1]


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


def test_dashboard_url_not_in_allowlist():
    """dashboard_url was removed from client-facing emails — must not be allowed."""
    assert "dashboard_url" not in ALLOWED_MERGE_TAGS


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
