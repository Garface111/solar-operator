import pytest
from api.adapters._gmp_clean import clean_gmp_nickname


@pytest.mark.parametrize("raw, expected", [
    # The reported pattern
    ("1a_name", "name"),
    ("1a_Chester", "Chester"),
    ("2b_Tannery Brook", "Tannery Brook"),
    ("10_Londonderry", "Londonderry"),
    ("1_Bruce Genereaux", "Bruce Genereaux"),

    # Already-clean → unchanged
    ("Chester", "Chester"),
    ("Tannery Brook Solar", "Tannery Brook Solar"),

    # All-caps shout → title-case
    ("WATERFORD ARRAY", "Waterford Array"),

    # No separator after digits → unchanged
    ("100kW South Field", "100kW South Field"),
    ("2024 Install", "2024 Install"),

    # Letter-only prefix → unchanged (might be intentional)
    ("a_Hidden Pond", "a_Hidden Pond"),

    # Edge cases
    (None, None),
    ("", ""),
    ("   ", "   "),  # whitespace-only preserved (caller decides)

    # If cleaning would yield empty → preserve original
    ("1_", "1_"),
    ("1a__", "1a__"),

    # Underscores within the body
    ("Chester_Solar", "Chester Solar"),
    ("Tannery_Brook_Solar", "Tannery Brook Solar"),

    # Multiple consecutive underscores collapse
    ("Chester__Solar", "Chester Solar"),
])
def test_clean_gmp_nickname(raw, expected):
    assert clean_gmp_nickname(raw) == expected
