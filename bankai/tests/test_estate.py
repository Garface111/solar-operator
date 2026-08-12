"""Estate & guardianship completeness: the vault checklist that tracks the gaps."""
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


def test_an_empty_vault_flags_the_urgent_gaps_first(session):
    status = estate.checklist_status(session)
    assert status["present_count"] == 0
    assert status["completeness_pct"] == 0
    # guardianship and a will lead the missing list
    top = [i["key"] for i in status["top_missing"]]
    assert top[0] == "guardianship" and "will" in top[:2]
    assert all(i["priority"] == "urgent" for i in status["top_missing"][:2])


def test_a_matched_document_marks_an_item_present(session):
    _doc(session, "Family Trust Agreement", category="estate",
         summary="Revocable trust agreement governing the Fidelity trust accounts")
    status = estate.checklist_status(session)
    by_key = {i["key"]: i for i in status["items"]}
    assert by_key["trust_agreement"]["present"] is True
    assert by_key["trust_agreement"]["document"]["title"].startswith("Family Trust")
    # something not on file stays missing
    assert by_key["guardianship"]["present"] is False


def test_matching_looks_at_summary_and_content_not_just_title(session):
    # a cryptically-titled file whose extracted text reveals what it is
    _doc(session, "LPP69-345-635", category="insurance",
         content="Homeowners insurance policy declarations page, dwelling coverage $700,000")
    status = estate.checklist_status(session)
    by_key = {i["key"]: i for i in status["items"]}
    assert by_key["homeowners"]["present"] is True


def test_a_full_folder_does_not_falsely_satisfy_an_item(session):
    # eight estate-category docs, none of which is a guardianship designation
    for i in range(8):
        _doc(session, f"estate misc {i}", category="estate", summary="beneficiary letter")
    status = estate.checklist_status(session)
    by_key = {i["key"]: i for i in status["items"]}
    assert by_key["guardianship"]["present"] is False  # category alone proves nothing
    # but 'beneficiary' in the summary does satisfy the beneficiaries item
    assert by_key["beneficiaries"]["present"] is True


def test_the_tool_round_trips(session):
    _doc(session, "Grant Deed - Cabrillo Dr", category="home",
         content="This Grant Deed conveys the property at 36001 Cabrillo Dr")
    out = json.loads(execute_tool(session, "estate_checklist", {}))
    assert out["total"] == len(estate.CHECKLIST)
    by_key = {i["key"]: i for i in out["items"]}
    assert by_key["deed"]["present"] is True
    assert "attorney" in out["note"]
