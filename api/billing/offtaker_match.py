"""
Fuzzy array-name matcher for the "flawless" offtaker upload (Ford, 2026-07-01).

BILLING-CRITICAL. A wrong array→offtaker match produces a wrong invoice, so this
module NEVER auto-commits a low-confidence guess. It returns a confidence class
and a ranked list of alternatives; anything below `high` is meant to surface for
operator review in the frontend, never to be silently written.

Pure + deterministic + dependency-free (stdlib difflib only) so it is trivially
unit-testable. See the __main__ self-test at the bottom.

The matching model is ARRAY-FIRST with a utility-bill override:
  * The operator's roster names ARRAYS (human names like "Maple Street (53984)").
  * We match that raw name against BOTH the tenant's Array names AND the
    array_name/nickname carried on each linked utility account.
  * The matched array's utility account (the one with a bill) is what the invoice
    is billed FROM. If the matched array has NO linked utility account with a
    bill, we return utility_account_id=null and flag it so review catches it —
    an offtaker can't be invoiced without a settled bill to price from.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

# Confidence thresholds. Tuned conservatively: `high` must be a near-certainty
# because a `high` match is eligible for auto-commit (ready) in the caller.
RATIO_HIGH = 0.88
RATIO_MEDIUM = 0.66

# A trailing parenthetical is almost always the NEPOOL-GIS id, e.g.
# "Maple Street Solar (53984)". Strip it before comparing names so the id
# doesn't dominate the ratio (or, worse, make two different arrays look alike).
_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Generic solar-site words that carry almost no identifying signal. A match on
# these alone (e.g. "Community Solar" ⊆ "Elm Street Community Solar") must NEVER
# reach auto-committable `high` — dozens of arrays share them. Only DISTINCTIVE
# (non-stopword) tokens count toward the ≥2-distinctive-token bar for `high`.
_STOPWORDS = frozenset({
    "solar", "community", "farm", "field", "street", "st", "road", "rd",
    "project", "site", "array", "power", "energy", "the", "of", "and",
})


def _distinctive(tokens: frozenset[str]) -> frozenset[str]:
    """Tokens that actually identify a site (drop generic solar-site words)."""
    return frozenset(t for t in tokens if t not in _STOPWORDS)


def _normalize(raw: Optional[str]) -> str:
    """lowercase → strip a trailing parenthetical → strip punctuation → collapse
    whitespace. Deterministic and total (None/'' → '')."""
    if not raw:
        return ""
    s = str(raw).lower().strip()
    s = _TRAILING_PAREN.sub("", s)
    # Replace any run of non-alphanumerics with a single space, then collapse.
    s = _NON_ALNUM.sub(" ", s)
    return " ".join(s.split())


def _tokens(norm: str) -> frozenset[str]:
    return frozenset(norm.split()) if norm else frozenset()


def _token_set_ratio(a_norm: str, b_norm: str) -> float:
    """Jaccard overlap of the two normalized token sets (0..1). Robust to word
    reordering and to one name being a superset of the other's words."""
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _whole_token_containment(a_norm: str, b_norm: str) -> bool:
    """True when one name's tokens are entirely contained in the other's (a whole
    superset/subset, not a substring-of-a-word). "maple street" vs
    "maple street solar" → True. Guards against the substring false-positive
    where "art" matches inside "sparta"."""
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta


def _seq_ratio(a_norm: str, b_norm: str) -> float:
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def _shared_distinctive(a_norm: str, b_norm: str) -> int:
    """Count of DISTINCTIVE (non-stopword) tokens shared by both names. This is
    the signal that separates a real site match ("Maple Street" ⊆ "Maple Street
    Solar" → {maple} … 1 distinctive) from a generic-word collision ("Community
    Solar" ⊆ "Elm Street Community Solar" → {} … 0 distinctive)."""
    da = _distinctive(_tokens(a_norm))
    db = _distinctive(_tokens(b_norm))
    return len(da & db)


def _classify(a_norm: str, b_norm: str) -> tuple[str, float, bool]:
    """Return (confidence, score, is_containment) for a single candidate name vs
    the raw name. `score` is the blended ranking number (higher = better).
    `is_containment` is True when the match reached `high` purely on whole-token
    containment (so the caller can detect a multi-array containment tie and demote
    it — see match_array). Confidence classes:
      exact  — normalized strings equal
      high   — seq ratio ≥ .88, OR whole-token containment backed by ≥2
               DISTINCTIVE (non-stopword) shared tokens
      medium — seq ratio ≥ .66, OR token-set overlap ≥ .66, OR whole-token
               containment on a single distinctive token (needs_review)
      none   — below all of the above

    A bare subset on generic solar-site words alone ("Community Solar") can never
    reach `high`: it lacks the ≥2 distinctive tokens, so it tops out at `medium`.
    """
    if not a_norm or not b_norm:
        return "none", 0.0, False
    if a_norm == b_norm:
        return "exact", 1.0, False
    seq = _seq_ratio(a_norm, b_norm)
    tok = _token_set_ratio(a_norm, b_norm)
    contained = _whole_token_containment(a_norm, b_norm)
    shared_distinctive = _shared_distinctive(a_norm, b_norm)
    # Containment only earns `high` when it's backed by real identifying signal:
    # at least two distinctive (non-stopword) tokens in common. A single
    # distinctive token — or only stopwords — is too weak to auto-commit.
    containment_high = contained and shared_distinctive >= 2
    # Blended score for ranking alternatives — take the strongest signal, then
    # nudge with the other so ties break sensibly. Containment is a strong signal.
    score = max(seq, tok)
    if containment_high:
        score = max(score, RATIO_HIGH)
    if seq >= RATIO_HIGH or containment_high:
        conf = "high"
    elif seq >= RATIO_MEDIUM or tok >= RATIO_MEDIUM or contained:
        # Containment on <2 distinctive tokens still deserves review (medium),
        # not silence — the operator confirms it in the frontend.
        conf = "medium"
    else:
        conf = "none"
    return conf, score, containment_high


# Confidence ordering for picking the single best candidate.
_CONF_RANK = {"exact": 3, "high": 2, "medium": 1, "none": 0}


def _utility_label(ua: dict) -> str:
    """A short human label for a utility account, e.g. "GMP · 12345 · Maple St"."""
    prov = (ua.get("provider") or "").upper()
    acct = ua.get("account_number") or ""
    nick = ua.get("nickname") or ""
    parts = [p for p in (prov, str(acct) if acct else "", nick) if p]
    return " · ".join(parts) if parts else (prov or "utility account")


def match_array(raw_name: str,
                arrays: list[dict],
                utility_accounts: list[dict]) -> dict:
    """Fuzzy-match a raw roster array name to one of the tenant's arrays.

    Args:
      raw_name: the array name as typed in the operator's roster.
      arrays: [{id, name}, ...] — the tenant's arrays.
      utility_accounts: [{utility_account_id, array_id, array_name, nickname,
                          provider, has_bill, account_number?}, ...].

    Returns a dict (see module docstring / task spec):
      { array_id, array_name, utility_account_id, utility_label, provider,
        confidence, alternatives: [{array_id, array_name, utility_account_id,
        utility_label}], flags: [...] }

    Deterministic + pure — no DB, no I/O. Every candidate name (array names AND
    utility-account array_name/nickname) is scored against the normalized raw
    name; the best-scoring array wins. If the best array has a linked utility
    account WITH a bill, that account is returned; otherwise utility_account_id
    is null and a "no_utility_bill" flag is set for review.
    """
    raw_norm = _normalize(raw_name)

    # Index utility accounts by array_id, and note which arrays have a billed one.
    ua_by_array: dict[int, list[dict]] = {}
    for ua in utility_accounts:
        aid = ua.get("array_id")
        if aid is None:
            continue
        ua_by_array.setdefault(int(aid), []).append(ua)

    # Build a superset of candidate arrays: every Array, PLUS any array referenced
    # only by a utility account (defensive — normally arrays already covers them).
    array_names: dict[int, str] = {}
    for a in arrays:
        if a.get("id") is None:
            continue
        array_names[int(a["id"])] = a.get("name") or ""
    for aid, uas in ua_by_array.items():
        if aid not in array_names:
            # Name this array from the first utility account that carries one.
            nm = next((u.get("array_name") for u in uas if u.get("array_name")), "")
            array_names[aid] = nm or ""

    # Score every candidate array. For each array the "name" we compare against is
    # the best of: the Array.name and each linked account's array_name/nickname.
    scored: list[tuple[int, str, float]] = []  # (array_id, best_conf, best_score)
    # Arrays that reached `high` PURELY on whole-token containment. If more than
    # one array lands here for the same raw name (e.g. two "* Community Solar"
    # sites both containing the raw "Community Solar"), the pick is an arbitrary
    # array_id tiebreak — exactly the silent-wrong-meter trap. We demote every
    # such contender to `medium` (→ needs_review) so BOTH surface for the
    # operator instead of one being auto-committed.
    containment_high_aids: list[int] = []
    for aid, aname in array_names.items():
        candidates = [aname]
        for ua in ua_by_array.get(aid, []):
            candidates.append(ua.get("array_name") or "")
            candidates.append(ua.get("nickname") or "")
        best_conf, best_score = "none", -1.0
        arr_containment_high = False
        for cand in candidates:
            cnorm = _normalize(cand)
            if not cnorm:
                continue
            conf, score, is_containment = _classify(raw_norm, cnorm)
            # Prefer higher confidence class first, then higher raw score.
            if (_CONF_RANK[conf], score) > (_CONF_RANK[best_conf], best_score):
                best_conf, best_score = conf, score
            if conf == "high" and is_containment:
                arr_containment_high = True
        if arr_containment_high:
            containment_high_aids.append(aid)
        scored.append((aid, best_conf, best_score))

    # Two-way (or more) containment tie → ambiguous. Demote each containment-only
    # `high` array to `medium` so none auto-commits and all surface for review.
    # Arrays that earned `high` some other way (exact/strong seq ratio) are left
    # alone — only the ambiguous bare-containment picks are pulled back.
    if len(containment_high_aids) > 1:
        ambiguous = set(containment_high_aids)
        scored = [
            (aid, "medium" if (aid in ambiguous and conf == "high") else conf, score)
            for (aid, conf, score) in scored
        ]

    # Rank: confidence class desc, then blended score desc, then array_id asc
    # (stable/deterministic tiebreak).
    scored.sort(key=lambda t: (_CONF_RANK[t[1]], t[2], -t[0]), reverse=True)

    def _pick_ua(aid: int) -> Optional[dict]:
        """Choose the utility account to bill from for this array: prefer one
        WITH a bill; else the first linked account; else None."""
        uas = ua_by_array.get(aid, [])
        if not uas:
            return None
        billed = [u for u in uas if u.get("has_bill")]
        if billed:
            return billed[0]
        return uas[0]

    flags: list[str] = []
    if not scored or scored[0][1] == "none" or scored[0][2] <= 0:
        # No usable match at all.
        alts = []
        for aid, _conf, _score in scored[:3]:
            ua = _pick_ua(aid)
            alts.append({
                "array_id": aid,
                "array_name": array_names.get(aid) or None,
                "utility_account_id": ua.get("utility_account_id") if ua else None,
                "utility_label": _utility_label(ua) if ua else None,
            })
        return {
            "array_id": None,
            "array_name": None,
            "utility_account_id": None,
            "utility_label": None,
            "provider": None,
            "confidence": "none",
            "alternatives": alts,
            "flags": ["no_match"],
        }

    best_aid, best_conf, _best_score = scored[0]
    best_ua = _pick_ua(best_aid)
    if best_ua is None:
        flags.append("no_utility_account")
    elif not best_ua.get("has_bill"):
        # Matched an array + account, but that account has no settled bill yet →
        # can't price an invoice from it. Surface for review, don't block silently.
        flags.append("no_utility_bill")

    # Alternatives = the next best 2-3 distinct arrays (for the review dropdown).
    alternatives = []
    for aid, _conf, _score in scored[1:4]:
        ua = _pick_ua(aid)
        alternatives.append({
            "array_id": aid,
            "array_name": array_names.get(aid) or None,
            "utility_account_id": ua.get("utility_account_id") if ua else None,
            "utility_label": _utility_label(ua) if ua else None,
        })

    return {
        "array_id": best_aid,
        "array_name": array_names.get(best_aid) or None,
        "utility_account_id": best_ua.get("utility_account_id") if best_ua else None,
        "utility_label": _utility_label(best_ua) if best_ua else None,
        "provider": (best_ua.get("provider") if best_ua else None),
        "confidence": best_conf,
        "alternatives": alternatives,
        "flags": flags,
    }


if __name__ == "__main__":
    # ── Deterministic self-test (run: python -m api.billing.offtaker_match) ──
    arrays = [
        {"id": 1, "name": "Maple Street Solar (53984)"},
        {"id": 2, "name": "Route 7 Community Array"},
        {"id": 3, "name": "Hilltop Farm"},
        {"id": 4, "name": "Riverside Solar Field"},
    ]
    uaccts = [
        {"utility_account_id": 101, "array_id": 1, "array_name": "Maple Street Solar",
         "nickname": "Maple St", "provider": "gmp", "account_number": "12345",
         "has_bill": True},
        {"utility_account_id": 102, "array_id": 2, "array_name": "Route 7 Community",
         "nickname": None, "provider": "gmp", "account_number": "22222",
         "has_bill": True},
        {"utility_account_id": 103, "array_id": 3, "array_name": "Hilltop Farm",
         "nickname": None, "provider": "vec", "account_number": "33333",
         "has_bill": False},  # no settled bill yet
        # array 4 (Riverside) has NO linked utility account at all
    ]

    failures = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        status = "ok " if cond else "FAIL"
        print(f"  [{status}] {label}" + (f"  — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    print("match_array self-test:")

    # 1. Exact normalized match (parenthetical GIS id stripped both sides).
    r = match_array("Maple Street Solar (53984)", arrays, uaccts)
    check("exact match strips GIS id", r["confidence"] == "exact" and r["array_id"] == 1,
          f"got {r['confidence']}/{r['array_id']}")
    check("exact match binds billed utility account",
          r["utility_account_id"] == 101, f"got {r['utility_account_id']}")

    # 2. Single-distinctive-token containment ("maple street" ⊆ array; only
    #    "maple" is distinctive — "street" is a stopword) → NOT auto-high. It
    #    tops out at medium/needs_review, and still resolves to the right array.
    r = match_array("Maple Street", arrays, uaccts)
    check("single-distinctive containment → medium not high",
          r["confidence"] == "medium" and r["array_id"] == 1,
          f"got {r['confidence']}/{r['array_id']}")

    # 3. Typo → high via sequence ratio.
    r = match_array("Maple Steet Solar", arrays, uaccts)
    check("typo → high/medium not none",
          r["confidence"] in ("high", "medium") and r["array_id"] == 1,
          f"got {r['confidence']}/{r['array_id']}")

    # 4. Reordered / partial tokens → medium, still right array.
    r = match_array("Community Route 7", arrays, uaccts)
    check("reordered tokens → matched array 2",
          r["array_id"] == 2, f"got {r['array_id']} conf={r['confidence']}")

    # 5. Totally unrelated → none, no array bound, alternatives offered.
    r = match_array("Z%%%z Nonexistent Plant", arrays, uaccts)
    check("garbage → none + no array", r["confidence"] == "none" and r["array_id"] is None,
          f"got {r['confidence']}/{r['array_id']}")
    check("garbage still offers alternatives", isinstance(r["alternatives"], list))

    # 6. Matched array whose account has NO bill → flagged, ua may be null-bill.
    r = match_array("Hilltop Farm", arrays, uaccts)
    check("hilltop matched", r["array_id"] == 3, f"got {r['array_id']}")
    check("hilltop flagged no_utility_bill", "no_utility_bill" in r["flags"],
          f"flags={r['flags']}")

    # 7. Matched array with NO utility account at all → no_utility_account flag.
    r = match_array("Riverside Solar Field", arrays, uaccts)
    check("riverside matched", r["array_id"] == 4, f"got {r['array_id']}")
    check("riverside no utility account + null id",
          "no_utility_account" in r["flags"] and r["utility_account_id"] is None,
          f"flags={r['flags']} ua={r['utility_account_id']}")

    # 8. Alternatives are distinct arrays, capped at 3.
    r = match_array("Maple", arrays, uaccts)
    alt_ids = [a["array_id"] for a in r["alternatives"]]
    check("alternatives ≤ 3", len(r["alternatives"]) <= 3, f"got {len(r['alternatives'])}")
    check("best not duplicated in alternatives", r["array_id"] not in alt_ids,
          f"best={r['array_id']} alts={alt_ids}")

    # 9. Substring-of-a-word must NOT be a false high (guard for containment).
    r = match_array("art", [{"id": 9, "name": "Sparta Field"}], [])
    check("substring-in-word not high",
          r["confidence"] != "high" or r["array_id"] is None,
          f"got {r['confidence']}/{r['array_id']}")

    # 10. Empty / None raw name → none, no crash.
    r = match_array("", arrays, uaccts)
    check("empty raw → none", r["confidence"] == "none" and r["array_id"] is None)

    # ── Adversarial cases for finding #2 (generic-token containment trap) ──

    # 11. (a) A bare generic name ("Community Solar") that is a whole-token subset
    #     of TWO distinct "* Community Solar" arrays must NOT auto-commit to one.
    #     Every shared token is a stopword (community, solar) → 0 distinctive, and
    #     two arrays tie on containment → demote to medium, surface BOTH.
    ambig_arrays = [
        {"id": 10, "name": "Elm Street Community Solar"},
        {"id": 11, "name": "Oak Street Community Solar"},
    ]
    r = match_array("Community Solar", ambig_arrays, [])
    check("generic 2-array containment NOT auto-high",
          r["confidence"] != "high" and r["confidence"] != "exact",
          f"got {r['confidence']}/{r['array_id']}")
    alt_ids_a = {r["array_id"]} | {a["array_id"] for a in r["alternatives"]}
    check("generic 2-array containment surfaces BOTH candidates",
          {10, 11} <= alt_ids_a, f"surfaced={sorted(x for x in alt_ids_a if x is not None)}")

    # 12. (b) A genuinely DISTINCTIVE 2-token match still resolves `high` cleanly.
    #     "Elm Street Community Solar" carries distinctive {elm} — only 1 — so use
    #     a raw name sharing TWO distinctive tokens with exactly one array.
    distinct_arrays = [
        {"id": 20, "name": "Elm Ridge Community Solar"},
        {"id": 21, "name": "Oak Street Community Solar"},
    ]
    r = match_array("Elm Ridge", distinct_arrays, [])
    check("distinctive 2-token containment → high",
          r["confidence"] == "high" and r["array_id"] == 20,
          f"got {r['confidence']}/{r['array_id']}")

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} case(s): {failures}")
        raise SystemExit(1)
    print("SELF-TEST PASSED — all cases green.")
