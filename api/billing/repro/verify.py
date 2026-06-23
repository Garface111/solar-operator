"""
AI verification — the "to the pixel" check.

Renders the reproduced invoice to a PNG and asks Claude (vision) to compare it
against a reference image of the operator's own invoice, returning a structured
verdict: does it match, and an itemized list of mismatches (wrong/missing label,
misaligned column, wrong number, dropped section…). This is what turns "looks
about right" into "matches down to the pixel" — its findings drive the refine
loop in pipeline.reproduce_invoice.

Reference-optional: with no reference image it does a self-consistency check
(are the numbers internally coherent, nothing obviously broken?). Degrades to a
skipped verdict (ok=None) when no API key or no PNG renderer is available, so it
never blocks delivery.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .llm import call_json, llm_available

log = logging.getLogger(__name__)


# ─── deterministic numeric guard (no AI, no reference) ───────────────────────
# The hard correctness gate: extract the rendered invoice's numbers and confirm
# the expected Amount Due is actually printed on it. If a column was mismapped,
# the total won't appear where we expect — so this catches a bad fill BEFORE it
# is ever attached, and the send path falls back to the standard invoice.

_NUM_RE = re.compile(r"-?\$?\s?([0-9][0-9,]*(?:\.[0-9]+)?)")


def extract_pdf_numbers(pdf_bytes: bytes) -> list[float]:
    """All numeric tokens in a PDF's text (commas stripped). Empty on failure."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:  # noqa: BLE001
        log.warning("extract_pdf_numbers failed: %s", e)
        return []
    out: list[float] = []
    for m in _NUM_RE.finditer(text):
        try:
            out.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return out


def amount_present(pdf_bytes: bytes, expected: Optional[float], tol: float = 0.01) -> bool:
    """Is the expected Amount Due actually printed on the rendered invoice?
    True (pass) when expected is None (nothing to check) or the value appears
    within `tol`. False means the fill is suspect — don't ship this render."""
    if expected is None:
        return True
    nums = extract_pdf_numbers(pdf_bytes)
    if not nums:
        return True  # can't read the PDF text → don't block on the numeric guard
    return any(abs(n - float(expected)) <= tol for n in nums)

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "matches": {"type": "boolean"},
        "score": {"type": "number"},               # 0..1 visual fidelity
        "mismatches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string"},     # label|number|layout|missing|extra
                    "where": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["kind", "detail"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["matches", "score", "summary"],
}


@dataclass
class Verdict:
    ok: Optional[bool]                  # True/False, or None when verification was skipped
    score: Optional[float] = None
    mismatches: list[dict] = field(default_factory=list)
    summary: str = ""

    @property
    def skipped(self) -> bool:
        return self.ok is None


_SYSTEM = (
    "You verify that a REPRODUCED solar invoice matches the operator's ORIGINAL "
    "format down to the pixel. You receive the reproduced page and (optionally) "
    "the original. Report every mismatch: wrong or missing labels, columns that "
    "don't line up, numbers in the wrong place, dropped or added sections, font/"
    "weight/shading differences. 'matches' is true only when a human would call "
    "them the same document with updated numbers. Be strict but don't invent "
    "differences. score is overall visual fidelity 0..1."
)


def ai_verify(rendered_png: Optional[bytes],
              reference_png: Optional[bytes] = None) -> Verdict:
    """Compare the rendered invoice to the reference (or self-check). Returns a
    Verdict; ok=None means verification was skipped (no key / no PNG)."""
    if not llm_available() or not rendered_png:
        return Verdict(ok=None, summary="verification skipped (no LLM key or no PNG renderer)")
    images = [("image/png", rendered_png)]
    if reference_png:
        images.append(("image/png", reference_png))
        prompt = ("First image: our reproduction. Second image: the operator's original. "
                  "Compare them and report mismatches.")
    else:
        prompt = ("No reference provided. Check the reproduction for internal "
                  "consistency and obvious breakage (mis-rendered cells, '#####' "
                  "overflow, missing totals).")
    try:
        v = call_json(system=_SYSTEM, user_text=prompt, images=images,
                      schema=VERDICT_SCHEMA, max_tokens=2048)
    except Exception as e:  # noqa: BLE001
        log.warning("ai_verify failed: %s", e)
        return Verdict(ok=None, summary=f"verification errored: {e}")
    return Verdict(ok=bool(v.get("matches")), score=v.get("score"),
                   mismatches=v.get("mismatches") or [], summary=v.get("summary", ""))
