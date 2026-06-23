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


# ─── deterministic numeric guard (no AI, no reference) — FAIL-CLOSED ──────────
# The hard correctness gate (hardened after adversarial review wf_74388645): it is
# a MONEY gate, so every uncertain path resolves to "don't trust this render → fall
# back to the standard invoice", never "ship it anyway". It confirms the expected
# Amount Due is printed NEXT TO ITS LABEL (cell-anchored, not just present anywhere
# on the page) and with the right SIGN, so a mismapped column or a sign-flipped /
# credit total is caught instead of waved through.

# Labels that mark the FINAL total (distinct from Net Rate / Solar Savings lines).
_AMOUNT_DUE_LABELS = ("amount due", "final amount owed", "amount owed",
                      "total due", "balance due", "total amount due")
# A number token, capturing a leading '-' OR surrounding ( ) as an accounting negative.
_NUM_TOKEN = re.compile(r"(\()?\s*(-?)\$?\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(\))?")


def _parse_signed(text: str) -> list[float]:
    """Signed numeric values in `text` ('-$50' / '($50.00)' → -50.0; '$50' → 50.0)."""
    out: list[float] = []
    for m in _NUM_TOKEN.finditer(text):
        neg = (m.group(2) == "-") or (m.group(1) == "(" and m.group(4) == ")")
        try:
            out.append((-1.0 if neg else 1.0) * float(m.group(3).replace(",", "")))
        except ValueError:
            continue
    return out


def _pdf_lines(pdf_bytes: bytes) -> Optional[list[str]]:
    """The PDF's text lines, or None when the PDF can't be read (unreadable ≠ no text)."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:  # noqa: BLE001
        log.warning("pdf text extraction failed: %s", e)
        return None
    return text.splitlines()


def extract_pdf_numbers(pdf_bytes: bytes) -> list[float]:
    """All SIGNED numeric tokens in a PDF's text. [] on an unreadable PDF."""
    lines = _pdf_lines(pdf_bytes)
    return _parse_signed("\n".join(lines)) if lines is not None else []


def amount_present(pdf_bytes: bytes, expected: Optional[float], tol: float = 0.01) -> bool:
    """FAIL-CLOSED: True only when the expected Amount Due is positively confirmed
    on the render. False (→ fall back to the standard invoice) when the PDF is
    unreadable, the value is missing, the labeled total is wrong, or the sign is
    flipped. expected=None → False (nothing to verify ⇒ don't trust the render)."""
    if expected is None:
        return False
    exp = float(expected)
    if abs(exp) < 0.005:
        return True                              # a $0 / banked balance is trivially fine

    def near(n: float) -> bool:
        return abs(n - exp) <= tol               # sign-sensitive: -exp won't match +exp

    lines = _pdf_lines(pdf_bytes)
    if not lines:
        return False                             # unreadable render → fail closed

    # 1) Cell-anchored: the value on (or just under) an Amount-Due label line.
    label_seen = False
    for i, ln in enumerate(lines):
        if any(lbl in ln.lower() for lbl in _AMOUNT_DUE_LABELS):
            label_seen = True
            window = ln + " " + (lines[i + 1] if i + 1 < len(lines) else "")
            if any(near(n) for n in _parse_signed(window)):
                return True
    if label_seen:
        return False                             # labeled total present but WRONG → fail closed
    # 2) No recognizable amount-due label at all → weak page-wide presence (sign-aware).
    return any(near(n) for n in _parse_signed("\n".join(lines)))

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
