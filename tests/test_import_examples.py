"""
Validate that every reference example in import_examples.py is well-formed:
- Every NEPOOL ID is numeric (digits only)
- Every login has a non-null utility
- Every client has at least one login
- Every array has a non-empty name
"""
from __future__ import annotations

import pytest

from api.import_examples import ALL_EXAMPLES


def _iter_logins(example: dict):
    for client in example.get("clients", []):
        yield from client.get("logins", [])


def _iter_arrays(example: dict):
    for login in _iter_logins(example):
        for account in login.get("accounts", []):
            yield from account.get("arrays", [])


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_example_has_operator_name(example):
    assert example.get("name"), "operator name must be non-empty"


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_example_has_clients(example):
    assert example.get("clients"), "example must have at least one client"


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_every_client_has_name(example):
    for client in example.get("clients", []):
        assert client.get("name"), f"client missing name in {example['name']}"


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_every_client_has_at_least_one_login(example):
    for client in example.get("clients", []):
        assert client.get("logins"), (
            f"client '{client.get('name')}' in '{example['name']}' has no logins"
        )


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_every_login_has_utility(example):
    for login in _iter_logins(example):
        assert login.get("utility") is not None, (
            f"login missing utility field in '{example['name']}'"
        )
        assert login["utility"] in ("gmp", "vec"), (
            f"unknown utility '{login['utility']}' in '{example['name']}'"
        )


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_every_array_has_name(example):
    for array in _iter_arrays(example):
        assert array.get("name"), f"array missing name in '{example['name']}'"


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=[e["name"] for e in ALL_EXAMPLES])
def test_nepool_ids_are_numeric_when_present(example):
    for array in _iter_arrays(example):
        gis = array.get("nepool_gis_id")
        if gis is not None:
            assert str(gis).isdigit(), (
                f"NEPOOL ID '{gis}' for array '{array.get('name')}' in "
                f"'{example['name']}' is not all digits"
            )


def test_all_examples_count():
    assert len(ALL_EXAMPLES) == 4, "expected exactly 4 reference examples"
