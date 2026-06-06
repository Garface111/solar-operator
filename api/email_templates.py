"""
Solar Operator — client report email rendering (V2, June 2026).

Single source of truth for how a per-client report email is composed: the
built-in default subject/body, the merge-tag substitution, and the resolution
of the From header + send mode from a tenant's customization settings.

Shared by:
  - api/delivery.py        (real sends)
  - api/account.py         (/v1/account/email-preview live preview)

We deliberately support SIMPLE merge-tag substitution ({{tag}}), not full
Jinja control flow. A stamping agent wants to drop their name and a sentence
in — not write loops. Keeping it to plain substitution means a malformed
template can never raise at send time. Unknown tags are left untouched so a
typo is visible rather than silently blanked.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

# The merge tags we advertise in the dashboard help text. Anything else in a
# template is left verbatim (see render_merge).
MERGE_TAGS = (
    "client_name", "tenant_name", "quarter", "arrays_count",
    "period_start", "period_end", "tenant_email",
    "tenant_email_line",
)

# Built-in defaults. These mirror the original hard-coded delivery email so an
# operator who never touches the settings gets exactly today's behavior.
DEFAULT_SUBJECT_TEMPLATE = (
    "{{client_name}} — generation report ({{period_start}} to {{period_end}})"
)

DEFAULT_SIGNOFF = (
    "<p>Thank you,<br>{{tenant_name}}{{tenant_email_line}}</p>"
)

DEFAULT_BODY_TEMPLATE = (
    "<p>Dear {{client_name}},</p>"
    "<p>Here is your quarterly NEPOOL-GIS report from {{period_start}} to"
    " {{period_end}}. Please reach out with any questions.</p>"
    "{{signoff}}"
)

_TAG_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Canonical allowlist of merge tags the email-render context provides.
# Any {{tag}} the operator (or the AI assistant) tries to insert that is
# NOT in this set will render as a verbatim {{whatever}} in the live email
# preview — which then ships to the client. To prevent the AI from
# inventing plausible-sounding but unsupported tags (e.g. {{quarter_total_mwh}},
# {{operator_signature}}), regenerate_template_via_ai() validates against
# this allowlist and rejects/strips unknown tags before the new template
# replaces the operator's working copy. Keep in sync with quarter_context()
# + the per-client/per-tenant fields stitched in at render time
# (api/account.py preview_email_template + api/delivery.py build_ctx).
ALLOWED_MERGE_TAGS = frozenset({
    "client_name",
    "quarter",
    "period_start",
    "period_end",
    "tenant_name",
    "tenant_email_line",
    "arrays_count",
    "signoff",
})


def extract_tags(template: str) -> set[str]:
    """Return the set of {{tag}} names referenced in a template string."""
    return set(_TAG_RE.findall(template))


def unknown_tags(template: str) -> set[str]:
    """Tags used in `template` that are NOT in the canonical allowlist."""
    return extract_tags(template) - ALLOWED_MERGE_TAGS


def render_merge(template: str, ctx: dict) -> str:
    """Replace {{tag}} (with optional inner whitespace) using ctx.

    Tags not present in ctx are left verbatim so a typo'd tag is visible in
    the output rather than silently producing an empty string."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx and ctx[key] is not None:
            return str(ctx[key])
        return m.group(0)
    return _TAG_RE.sub(sub, template)


def html_to_text(html: str) -> str:
    """Crude HTML → plain-text for the multipart text/plain alternative.

    Good enough for our simple <p>/<b>/<a> bodies: drop tags, turn block
    boundaries into newlines, collapse runs of blank lines."""
    s = re.sub(r"(?i)</p\s*>", "\n\n", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&nbsp;", " "))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def quarter_context(ref: Optional[date] = None) -> dict:
    """Compute the most-recent COMPLETE calendar quarter as of `ref`.

    Returns {quarter, period_start, period_end} where quarter is like
    '2026 Q2' and the period_* are human dates bounding that quarter. This is
    the headline quarter for a report; the workbook itself still carries a
    rolling 6 quarters."""
    ref = ref or datetime.utcnow().date()
    # Quarter index 1..4 of the quarter ref falls in.
    cur_q = (ref.month - 1) // 3 + 1
    year, q = ref.year, cur_q - 1
    if q == 0:  # we're in Q1 → most recent complete quarter is last year's Q4
        year, q = year - 1, 4
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    # First day of the following month minus one day = last day of the quarter.
    next_month = date(year + (1 if end_month == 12 else 0),
                      1 if end_month == 12 else end_month + 1, 1)
    end = next_month - timedelta(days=1)
    return {
        "quarter": f"{year} Q{q}",
        "period_start": _fmt_date(start),
        "period_end": _fmt_date(end),
        "_start_date": start,
        "_end_date": end,
    }


def _fmt_date(d: date) -> str:
    """'Jun 30, 2026' with no zero-padded day, portable across platforms."""
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def build_context(*, client_name: str, tenant_name: str, arrays_count: int,
                  tenant_email: str = "",
                  ref: Optional[date] = None,
                  signoff_template: Optional[str] = None) -> dict:
    """Assemble the merge-tag context for one client's email."""
    qc = quarter_context(ref)
    email = tenant_email.strip()
    tenant_email_line = f"<br>{email}" if email else ""
    # Resolve {{signoff}}: render the signoff template's own merge tags first
    # (tenant_name, tenant_email_line) so the body can use {{signoff}} as a
    # fully-rendered block. Two-pass: signoff → ctx → body.
    signoff_t = (signoff_template or "").strip() or DEFAULT_SIGNOFF
    rendered_signoff = render_merge(signoff_t, {
        "tenant_name": tenant_name,
        "tenant_email": email,
        "tenant_email_line": tenant_email_line,
    })
    return {
        "client_name": client_name,
        "tenant_name": tenant_name,
        "quarter": qc["quarter"],
        "arrays_count": arrays_count,
        "period_start": qc["period_start"],
        "period_end": qc["period_end"],
        "tenant_email": email,
        "tenant_email_line": tenant_email_line,
        "signoff": rendered_signoff,
    }


def render_email(*, subject_template: Optional[str],
                 body_template: Optional[str], ctx: dict) -> tuple[str, str, str]:
    """Render (subject, html, text). Falls back to the built-in default for
    any template that is None/blank."""
    subj_t = (subject_template or "").strip() or DEFAULT_SUBJECT_TEMPLATE
    body_t = (body_template or "").strip() or DEFAULT_BODY_TEMPLATE
    subject = render_merge(subj_t, ctx)
    html = render_merge(body_t, ctx)
    text = html_to_text(html)
    return subject, html, text


def resolve_from_header(send_from_email: Optional[str],
                        send_from_name: Optional[str],
                        tenant_name: Optional[str]) -> Optional[str]:
    """Build the Resend `from` header from a tenant's settings, or None to use
    the platform default. Display name defaults to the tenant name."""
    email = (send_from_email or "").strip()
    if not email:
        return None
    name = (send_from_name or tenant_name or "").strip()
    return f"{name} <{email}>" if name else email


# ── AI template regeneration ──────────────────────────────────────────────────

_TEMPLATE_SYSTEM_PROMPT = (
    "You are a writing assistant helping a solar energy consultant customize "
    "their client report email.\n\n"
    "THE ONLY MERGE TAGS THAT EXIST IN THE SYSTEM ARE:\n"
    "  {{client_name}}, {{quarter}}, {{period_start}}, {{period_end}},\n"
    "  {{tenant_name}}, {{tenant_email_line}}, {{arrays_count}},\n"
    "  {{signoff}}\n\n"
    "CRITICAL RULES:\n"
    "- You may ONLY use the tags listed above. Do NOT invent new merge tags "
    "like {{quarter_total_mwh}}, {{operator_signature}}, {{client_address}}, "
    "{{rec_count}}, or anything else not in the list — they DO NOT EXIST in "
    "this system and will render as raw broken text in the customer's inbox.\n"
    "- If the operator asks for content backed by a value that isn't in the "
    "allowlist, write the sentence in plain prose instead of using a merge "
    "tag, OR tell them in your 'reply' field that the value isn't available "
    "yet and suggest the closest supported tag.\n"
    "- Preserve any allowlisted {{...}} merge tags exactly — never remove, "
    "rename, or modify them.\n"
    "- Do not change the greeting to 'Dear {{client_name}}' unless the operator "
    "explicitly asks for formal tone.\n"
    "- The body is simple HTML: use only <p>, <br>, <a href='...'>, <b>, <em> tags.\n"
    "- Keep the professional tone appropriate for a regulated energy market.\n\n"
    "Respond ONLY with a JSON object, NO markdown fences, with this exact shape:\n"
    '{"reply": "Brief 1-2 sentence description of what you changed", '
    '"body": "complete updated HTML body template", '
    '"subject": null}'
    "\nSet subject to null when unchanged, or to the new subject string if you changed it."
)


def regenerate_template_via_ai(
    *,
    current_body: str,
    current_subject: str,
    messages: list[dict],
    api_key: str,
) -> dict:
    """Call Anthropic to regenerate the email template body/subject.

    messages is the full conversation history [{role, content}].
    Returns {'reply': str, 'body': str, 'subject': str | None}.
    Raises httpx.HTTPStatusError on API failure, ValueError on bad JSON.
    """
    model = os.getenv("INGEST_LLM_MODEL", "claude-sonnet-4-5")
    system = (
        f"{_TEMPLATE_SYSTEM_PROMPT}\n\n"
        f"Current subject template:\n{current_subject}\n\n"
        f"Current body template:\n{current_body}"
    )
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    content = "".join(b.get("text", "") for b in body.get("content", []))
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            result = json.loads(raw[start : end + 1])
        else:
            raise ValueError("LLM response did not contain valid JSON")
    return {
        "reply": _augment_reply_with_strip_notice(
            str(result.get("reply") or "Updated."),
            stripped_subject=_strip_unknown_tags_collect(
                result.get("subject"), label="subject"
            ),
            stripped_body=_strip_unknown_tags_collect(
                result.get("body"), label="body"
            ),
        ),
        "body": _strip_unknown_tags(str(result.get("body") or current_body)),
        "subject": (
            _strip_unknown_tags(result["subject"])
            if isinstance(result.get("subject"), str)
            else result.get("subject")
        ),
    }


# ── Server-side guardrail: strip any merge tag the AI invented ────────────────

def _strip_unknown_tags(template: str | None) -> str | None:
    """Replace any non-allowlisted {{tag}} in `template` with neutral prose.

    The system prompt tells the LLM not to invent tags, but we treat that as
    a soft constraint — defense in depth turns any escaped {{whatever}} into
    a blank placeholder so the operator never ships a broken email to a
    client. The phrase we substitute ('[…]') is intentionally bland so it
    surfaces visibly in the preview and the operator notices the gap.
    """
    if not isinstance(template, str):
        return template

    def replace(m: re.Match) -> str:
        key = m.group(1)
        if key in ALLOWED_MERGE_TAGS:
            return m.group(0)
        return "[…]"

    return _TAG_RE.sub(replace, template)


def _strip_unknown_tags_collect(template, *, label: str) -> list[str]:
    if not isinstance(template, str):
        return []
    return sorted(unknown_tags(template))


def _augment_reply_with_strip_notice(
    reply: str, *, stripped_subject: list[str], stripped_body: list[str]
) -> str:
    """If the AI tried to insert tags that don't exist, tell the operator."""
    bad = sorted(set(stripped_subject) | set(stripped_body))
    if not bad:
        return reply
    listed = ", ".join("{{" + t + "}}" for t in bad[:4])
    if len(bad) > 4:
        listed += f", +{len(bad) - 4} more"
    suffix = (
        f"\n\nNote: I tried to use {listed} but those merge tags don't "
        "exist in this system — I replaced them with [...] placeholders "
        "you can edit by hand or rephrase."
    )
    return reply.rstrip() + suffix
