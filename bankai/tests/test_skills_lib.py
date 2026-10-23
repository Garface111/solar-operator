import pytest

from bankai import skills_lib

EXPECTED = {
    "consumer_protection",
    "insurance_claims",
    "negotiation",
    "subscriptions_darkpatterns",
    "tax_levers",
}


def test_list_skills_covers_the_shipped_packs():
    names = [s["name"] for s in skills_lib.list_skills()]
    assert EXPECTED.issubset(set(names))
    assert names == sorted(names)  # stable ordering for the agent's index


def test_every_skill_exposes_an_indexable_when_to_use():
    for skill in skills_lib.list_skills():
        assert skill["when_to_use"], f"{skill['name']} has no WHEN TO USE header"
        assert "WHEN TO USE" not in skill["when_to_use"]  # label stripped
        assert len(skill["when_to_use"]) > 60, skill["name"]
        assert skill["title"], f"{skill['name']} has no H1 title"


def test_skills_are_substantive():
    for skill in skills_lib.list_skills():
        assert skill["words"] >= 600, f"{skill['name']} is too thin: {skill['words']} words"


def test_read_skill_returns_full_text_starting_with_the_header():
    text = skills_lib.read_skill("negotiation")
    assert text.startswith("WHEN TO USE:")
    assert "# Bill negotiation and retention playbooks" in text
    assert len(text.split()) > 600


def test_read_skill_matches_the_listed_when_to_use():
    listed = {s["name"]: s["when_to_use"] for s in skills_lib.list_skills()}
    for name, when in listed.items():
        assert skills_lib._parse_when_to_use(skills_lib.read_skill(name)) == when


def test_tax_pack_carries_the_not_tax_advice_warning():
    text = skills_lib.read_skill("tax_levers")
    assert "NOT TAX ADVICE" in text
    assert "verify" in text.lower()


def test_read_skill_unknown_name_raises_valueerror():
    with pytest.raises(ValueError) as exc:
        skills_lib.read_skill("does_not_exist")
    assert "does_not_exist" in str(exc.value)
    assert "negotiation" in str(exc.value)  # error lists what IS available


@pytest.mark.parametrize("bad", ["../models", "negotiation.md", "/etc/passwd", "", "a b"])
def test_read_skill_rejects_path_like_names(bad):
    with pytest.raises(ValueError):
        skills_lib.read_skill(bad)


def test_parse_when_to_use_handles_markdown_decorated_headers():
    raw = "# WHEN TO USE: first line here\n> second line here\n\n# Title\nbody"
    assert skills_lib._parse_when_to_use(raw) == "first line here second line here"


def test_parse_when_to_use_stops_at_a_heading():
    raw = "WHEN TO USE: only one line\n# Title\nbody"
    assert skills_lib._parse_when_to_use(raw) == "only one line"


def test_parse_when_to_use_missing_header_is_empty_not_an_error():
    assert skills_lib._parse_when_to_use("# Title\nbody") == ""
