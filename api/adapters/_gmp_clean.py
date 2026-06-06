"""Heuristic cleaners for raw GMP nicknames.

GMP's internal portal stores account labels like "1a_Chester" or
"2b_Tannery Brook" where the leading "<digit><optional letter>_" is
GMP's positional code. Most operators don't notice these prefixes in
the GMP UI but they leak into Solar Operator surfaces (sandbox cards,
report headers, Excel sheet tabs) and look like a bug.

We strip these at the adapter boundary so every downstream consumer
sees clean text. Conservative — only strip when the pattern matches
clearly; otherwise pass through untouched.
"""
from __future__ import annotations

import re

# Matches "1_", "1a_", "12_", "12b_", "1-", "1a-" at the start of a string,
# only when a non-whitespace character follows (so "1_" alone is left untouched).
_GMP_POSITIONAL_PREFIX = re.compile(r"^\d{1,3}[a-zA-Z]?[_\-]+(?=\S)")

# Positional code with no real body: "1_", "1a__", "12b-", etc. — leave untouched.
_GMP_CODE_ONLY = re.compile(r"^\d{1,3}[a-zA-Z]?[_\-]+\s*$")

# Single letter followed by separator at the start: "a_…" — could be intentional.
_LETTER_PREFIX = re.compile(r"^[a-zA-Z][_\-]")

# Whitespace collapse
_WS = re.compile(r"\s+")


def clean_gmp_nickname(raw: str | None) -> str | None:
    """Return a sanitized array/account name from GMP's raw nickname.

    Returns None unchanged. Returns "" unchanged (don't lie about empty).
    Otherwise:
      1. Strip leading "<digit><opt letter><_-> " positional codes.
      2. Replace underscores with spaces (GMP often glues "Chester_Solar").
      3. Collapse whitespace.
      4. Title-case if the result is ALL-CAPS shouting (likely a forgotten
         legacy label); leave mixed-case alone.
      5. Strip leading/trailing whitespace.

    Conservative: passes through untouched when:
      - The string is just a positional code with no body (e.g. "1_").
      - The string starts with a letter-only prefix (e.g. "a_Hidden Pond") —
        might be intentional operator labelling.

    If the cleaning would yield an empty or whitespace-only result, return
    the original string (better stale than nothing).
    """
    if raw is None or raw == "":
        return raw
    # Letter-only prefix ("a_…") — could be intentional; leave untouched.
    if _LETTER_PREFIX.match(raw):
        return raw
    # Positional code with no meaningful body ("1_") — leave untouched.
    if _GMP_CODE_ONLY.match(raw):
        return raw
    s = _GMP_POSITIONAL_PREFIX.sub("", raw)
    s = s.replace("_", " ")
    s = _WS.sub(" ", s).strip()
    # All-caps shout → title-case
    if s and s == s.upper() and any(c.isalpha() for c in s):
        s = s.title()
    return s if s else raw
