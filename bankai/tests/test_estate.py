"""Estate & guardianship completeness: confirmed vs candidate vs missing."""
import json

from bankai import estate
from bankai.agent.tools import execute_tool
from bankai.models import Document


def _doc(session, title, category="other", summary="", content=""):
    d = Document(title=title, category=category, summary=summary, content_text=content,
                 sha256=f"h{abs(hash(title))%10**12}")
    session.add(d)
    session.flush()
    return d


def _by_key(status):
    return {i["key"]: i for i in status["items"]}


def test_an_empty_vault_flags_the_urgent_gaps_first(session):
    status = estate.checklist_status(session)
    assert status["confirmed_count"] == 0 and status["completeness_pct"] == 0
    top = [i["key"] for i in status["top_action"]]
    assert top[0] == "guardianship" and "will" in top[:2]


def test_a_title_match_is_confirmed(session):
    _doc(session, "Family Trust Agreement", category="estate",
         summary="Revocable trust governing the Fidelity accounts")
    by = _by_key(estate.checklist_status(session))
    assert by["trust_agreement"]["status"] == "confirmed"
    assert by["guardianship"]["status"] == "missing"


def test_a_summary_only_match_is_a_candidate_not_confirmed(session):
    # the surrogacy contract's summary mentions guardianship, but it is NOT a
    # guardianship designation — it must be a candidate to verify, never confirmed
    _doc(session, "Signed Surrogacy Contract", category="contract",
         summary="Gestational surrogacy agreement; addresses parentage and guardianship")
    by = _by_key(estate.checklist_status(session))
    assert by["surrogacy_agreement"]["status"] == "confirmed"     # title says surrogacy
    assert by["guardianship"]["status"] == "possible"             # only a summary mention
    # a candidate does not count toward completeness
    assert by["guardianship"] in [i for i in by.values() if i["status"] == "possible"]


def test_body_text_only_matches_inside_the_expected_category(session):
    # a TAX return whose text mentions 'trust' must NOT pass for the trust agreement
    _doc(session, "Federal tax return 2025", category="tax",
         content="Grantor trust income on Schedule E, revocable trust distributions")
    by = _by_key(estate.checklist_status(session))
    assert by["trust_agreement"]["status"] == "missing"


def test_body_text_within_category_is_a_candidate(session):
    _doc(session, "LPP69-345-635", category="insurance",
         content="Homeowners insurance policy declarations, dwelling coverage $700,000")
    by = _by_key(estate.checklist_status(session))
    assert by["homeowners"]["status"] == "possible"


def test_the_tool_round_trips(session):
    _doc(session, "Grant Deed - Cabrillo Dr", category="home",
         content="This Grant Deed conveys the property")
    out = json.loads(execute_tool(session, "estate_checklist", {}))
    assert out["total"] == len(estate.CHECKLIST)
    by = {i["key"]: i for i in out["items"]}
    assert by["deed"]["status"] == "confirmed"  # title has 'grant deed'
    assert "attorney" in out["note"]
