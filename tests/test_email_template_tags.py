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
    derive_greeting,
    extract_tags,
    looks_like_organization,
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


def test_default_body_uses_greeting():
    assert "{{greeting}}" in DEFAULT_BODY_TEMPLATE
    assert "{{client_first_name}}" not in DEFAULT_BODY_TEMPLATE
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


# ── looks_like_organization ───────────────────────────────────────────────────

def test_llo_person_two_tokens():
    assert looks_like_organization("Bruce Genereaux") is False


def test_llo_person_single_word():
    assert looks_like_organization("Madonna") is False


def test_llo_person_last_first_format():
    assert looks_like_organization("Genereaux, Bruce") is False


def test_llo_org_four_tokens():
    assert looks_like_organization("Green Mountain Community Solar") is True


def test_llo_org_community_token():
    assert looks_like_organization("Valley Community Farm") is True


def test_llo_org_solar_token():
    assert looks_like_organization("Tannery Brook Solar") is True


def test_llo_org_town_token():
    assert looks_like_organization("Town of Chester") is True


def test_llo_org_llc_suffix():
    assert looks_like_organization("Wilcox Inn LLC") is True


def test_llo_org_inc_suffix():
    assert looks_like_organization("Acme Energy Inc.") is True


def test_llo_org_coop():
    assert looks_like_organization("Northeast Co-Op") is True


def test_llo_org_ampersand():
    assert looks_like_organization("Smith & Sons") is True


def test_llo_org_school_signal():
    assert looks_like_organization("Brattleboro Elementary") is True


def test_llo_org_school_district():
    assert looks_like_organization("Windsor School District") is True


def test_llo_person_single_short_word():
    # No signal — treated as person (known edge case: Apple, Costco, etc.)
    assert looks_like_organization("Apple") is False


def test_llo_empty_and_whitespace():
    assert looks_like_organization("") is False
    assert looks_like_organization("   ") is False


def test_llo_org_energy_token():
    assert looks_like_organization("Green Energy") is True


def test_llo_person_three_tokens_no_signal():
    # Three common-word tokens but none are org signals
    assert looks_like_organization("James Arthur Brown") is False


# ── derive_greeting ───────────────────────────────────────────────────────────

def test_greeting_person():
    assert derive_greeting("Bruce Genereaux") == "Hi Bruce"


def test_greeting_last_first_format():
    assert derive_greeting("Genereaux, Bruce") == "Hi Bruce"


def test_greeting_single_name():
    assert derive_greeting("Madonna") == "Hi Madonna"


def test_greeting_org_four_tokens():
    assert derive_greeting("Green Mountain Community Solar") == "Dear Green Mountain Community Solar"


def test_greeting_org_town():
    assert derive_greeting("Town of Chester") == "Dear Town of Chester"


def test_greeting_org_llc():
    assert derive_greeting("Wilcox Inn LLC") == "Dear Wilcox Inn LLC"


def test_greeting_org_solar():
    assert derive_greeting("Tannery Brook Solar") == "Dear Tannery Brook Solar"


# ── greeting in allowlist and context ────────────────────────────────────────

def test_greeting_in_allowlist():
    assert "greeting" in ALLOWED_MERGE_TAGS


def test_build_context_includes_greeting_person():
    ctx = build_context(client_name="Bruce Genereaux", tenant_name="Solar Co", arrays_count=1)
    assert ctx["greeting"] == "Hi Bruce"


def test_build_context_includes_greeting_org():
    ctx = build_context(
        client_name="Green Mountain Community Solar",
        tenant_name="Solar Co",
        arrays_count=3,
    )
    assert ctx["greeting"] == "Dear Green Mountain Community Solar"


def test_render_email_default_template_person_greeting():
    ctx = build_context(client_name="Bruce Genereaux", tenant_name="Solar Co", arrays_count=2)
    _, html, _ = render_email(subject_template=None, body_template=None, ctx=ctx)
    assert "Hi Bruce," in html


def test_render_email_default_template_org_greeting():
    ctx = build_context(
        client_name="Green Mountain Community Solar",
        tenant_name="Solar Co",
        arrays_count=5,
    )
    _, html, _ = render_email(subject_template=None, body_template=None, ctx=ctx)
    assert "Dear Green Mountain Community Solar," in html


# ── regenerate_template_via_ai: non-JSON LLM replies must not 500 ─────────────
# Sentry PYTHON-FASTAPI-17: ValueError "LLM response did not contain valid JSON"
# when the model declined in prose (or returned empty content). The helper must
# surface that text as the assistant reply and leave the template unchanged.


class _FakeAnthropicResp:
    def __init__(self, text: str, status_code: int = 200):
        self.status_code = status_code
        self._text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                "err", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self):
        return {"content": [{"type": "text", "text": self._text}]}


def _call_regen(monkeypatch, llm_text: str, *, current_body: str = "<p>Original</p>"):
    import httpx
    from api.email_templates import regenerate_template_via_ai

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeAnthropicResp(llm_text)
    )
    return regenerate_template_via_ai(
        current_body=current_body,
        current_subject="Subj {{client_name}}",
        messages=[{"role": "user", "content": "Make it warmer"}],
        api_key="sk-test",
    )


def test_regen_prose_decline_does_not_raise(monkeypatch):
    """Polite prose decline (the live-demo muffin-recipe case) stays a soft no."""
    prose = (
        "I can't put a muffin recipe in a solar generation report email — "
        "tell me what you'd like to change about the greeting or tone."
    )
    out = _call_regen(monkeypatch, prose)
    assert prose in out["reply"]
    assert out["body"] == "<p>Original</p>"
    assert out["subject"] is None


def test_regen_empty_llm_content_does_not_raise(monkeypatch):
    """Empty model content used to raise JSONDecodeError → ValueError 502."""
    out = _call_regen(monkeypatch, "")
    assert out["body"] == "<p>Original</p>"
    assert out["subject"] is None
    assert isinstance(out["reply"], str) and out["reply"]
    assert "JSON" not in out["reply"]


def test_regen_malformed_braced_garbage_does_not_raise(monkeypatch):
    out = _call_regen(monkeypatch, "Sure! {not: valid json, trailing")
    assert out["body"] == "<p>Original</p>"
    assert "Sure!" in out["reply"] or out["reply"]


def test_regen_valid_json_still_applies(monkeypatch):
    payload = (
        '{"reply": "Tightened the tone.", '
        '"body": "<p>Hi {{greeting}},</p>", '
        '"subject": null}'
    )
    out = _call_regen(monkeypatch, payload)
    assert out["reply"] == "Tightened the tone."
    assert out["body"] == "<p>Hi {{greeting}},</p>"
    assert out["subject"] is None


def test_regen_fenced_json_still_applies(monkeypatch):
    payload = (
        "```json\n"
        '{"reply": "Ok.", "body": "<p>New</p>", "subject": "Q report"}\n'
        "```"
    )
    out = _call_regen(monkeypatch, payload)
    assert out["body"] == "<p>New</p>"
    assert out["subject"] == "Q report"


# ── regenerate_template_via_ai: Anthropic 400 must not 502 ────────────────────
# Sentry PYTHON-FASTAPI-10: Client error '400 Bad Request' for
# https://api.anthropic.com/v1/messages on offtaker email-template/chat.
# Soft-fail with template unchanged (same UX as prose declines).


def test_regen_anthropic_400_soft_fails(monkeypatch):
    import httpx
    from api.email_templates import regenerate_template_via_ai

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeAnthropicResp("bad", status_code=400)
    )
    out = regenerate_template_via_ai(
        current_body="<p>Original</p>",
        current_subject="Subj",
        messages=[{"role": "user", "content": "testing"}],
        api_key="sk-test",
    )
    assert out["body"] == "<p>Original</p>"
    assert out["subject"] is None
    assert isinstance(out["reply"], str) and out["reply"]
    assert "writing assistant" in out["reply"].lower() or "try again" in out["reply"].lower()


def test_normalize_anthropic_messages_merges_consecutive_users():
    from api.email_templates import _normalize_anthropic_messages

    out = _normalize_anthropic_messages([
        {"role": "user", "content": "make it warmer"},
        {"role": "user", "content": "  and shorter  "},
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": ""},  # dropped
        {"role": "user", "content": "thanks"},
    ])
    assert out == [
        {"role": "user", "content": "make it warmer\n\nand shorter"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "thanks"},
    ]


def test_normalize_anthropic_messages_drops_leading_assistant():
    from api.email_templates import _normalize_anthropic_messages

    out = _normalize_anthropic_messages([
        {"role": "assistant", "content": "stale"},
        {"role": "user", "content": "hi"},
    ])
    assert out == [{"role": "user", "content": "hi"}]


def test_regen_sends_normalized_messages(monkeypatch):
    """Consecutive user turns must be merged before the Anthropic POST."""
    import httpx
    from api.email_templates import regenerate_template_via_ai

    captured = {}

    def fake_post(*a, **k):
        captured["json"] = k.get("json") or (a[1] if len(a) > 1 else None)
        return _FakeAnthropicResp(
            '{"reply": "Ok.", "body": "<p>New</p>", "subject": null}'
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    regenerate_template_via_ai(
        current_body="<p>Original</p>",
        current_subject="Subj",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ],
        api_key="sk-test",
    )
    assert captured["json"]["messages"] == [
        {"role": "user", "content": "first\n\nsecond"},
    ]
